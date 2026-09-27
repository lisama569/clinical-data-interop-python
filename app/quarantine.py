"""被拒记录待处理区：分类持久化、分页查阅、容量淘汰与重放并发控制。

设计要点：
- 进入：被拒记录按失败类别（malformed_format / missing_required_field / unknown_code）
  持久化，保留脱敏上下文（source_fragment / masked_payload）；原始报文仅存于
  ``payload`` 字段供重放，任何对外视图都不得包含它。**任何新被拒记录都保证能入区**：
  容量不足时先淘汰为新记录腾出位置，绝不静默丢弃。
- 退出：重放判定通过 -> resolved（记录仍保留用于追溯）；仍失败 -> 保持 pending 并
  更新为最新失败类别与依据。
- 容量：在区（pending+resolved）记录数受容量上限约束；超限时按确定顺序淘汰：
  优先 created_at 升序（序号决胜）的最早 pending（正被重放占用者最后考虑），
  没有可淘汰 pending 时才淘汰最早的 resolved。被淘汰者转为 ``evicted`` 档案**继续
  保留**：失败类别、脱敏上下文与完整处理历史均可追溯，只清除不可外泄、也不再需要的
  原始报文；淘汰绝不改动其他在区记录的失败类别。
- 并发：所有状态变更在同一把锁下完成；acquire/release 保证同一条记录同一时刻只被
  一个重放处理，重复或并发触发得到可区分结论。
"""
from __future__ import annotations

import itertools
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from app.models import (
    HistoryEvent,
    LifecycleAction,
    PendingRecord,
    PendingStatus,
    RejectReason,
)

logger = logging.getLogger("clinical_interop.quarantine")

# 默认待处理区容量上限（只统计在区的 pending 与 resolved；evicted 档案不占容量）
DEFAULT_CAPACITY = 1000


class QuarantineFullError(Exception):
    """待处理区无法为新记录腾出位置。

    当前容量规则保证任何被拒记录都能入区（总会淘汰出位置），因此正常流程不会抛出；
    保留异常类型供容量策略扩展与既有调用方兼容。
    """


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuarantineStore:
    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._capacity = max(1, int(capacity))
        self._clock = clock or _utcnow_iso
        self._records: dict[str, PendingRecord] = {}
        self._busy: set[str] = set()
        self._seq = itertools.count()

    # -- 进入 ---------------------------------------------------------------
    def add_rejected(
        self,
        *,
        source_id: str,
        batch_id: str,
        format: str,
        reason: RejectReason,
        detail: str,
        source_fragment: str,
        masked_payload: str,
        payload: str,
        fingerprint: str,
    ) -> tuple[PendingRecord, bool]:
        """登记一条被拒记录；同指纹已有 pending 记录时幂等复用。

        返回 (记录, 是否新建)。新被拒记录保证能入区：容量不足时按规则淘汰一条
        在区记录为其腾出位置，被淘汰者转为 evicted 档案继续可追溯。
        """
        now = self._clock()
        with self._lock:
            existing = self._find_pending_by_fingerprint(fingerprint)
            if existing is not None:
                existing.updated_at = now
                existing.history.append(
                    HistoryEvent(
                        at=now,
                        action=LifecycleAction.REJECTED,
                        detail="重复进入待处理区，复用既有记录（幂等，不新增）",
                        reason=existing.reason.value,
                    )
                )
                logger.info(
                    "pending_id=%s 重复进入 幂等复用 类别=%s",
                    existing.pending_id,
                    existing.reason.value,
                )
                return existing, False

            self._ensure_room()
            record = PendingRecord(
                pending_id=uuid.uuid4().hex,
                source_id=source_id,
                batch_id=batch_id,
                format=format,
                reason=reason,
                detail=detail,
                source_fragment=source_fragment,
                masked_payload=masked_payload,
                payload=payload,
                fingerprint=fingerprint,
                seq=next(self._seq),
                status=PendingStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            record.history.append(
                HistoryEvent(
                    at=now,
                    action=LifecycleAction.REJECTED,
                    detail=detail,
                    reason=reason.value,
                )
            )
            self._records[record.pending_id] = record
            logger.info(
                "pending_id=%s 进入待处理区 来源=%s 类别=%s",
                record.pending_id,
                source_id,
                reason.value,
            )
            return record, True

    def _find_pending_by_fingerprint(self, fingerprint: str) -> PendingRecord | None:
        for r in self._records.values():
            if r.status is PendingStatus.PENDING and r.fingerprint == fingerprint:
                return r
        return None

    def _ensure_room(self) -> None:
        """容量满时按确定顺序淘汰一条在区记录，为新被拒记录腾出位置。

        顺序：最早的非占用 pending（created_at 升序、seq 决胜）；不存在时退而淘汰
        最早的 pending（含正被重放占用者）；再不存在才淘汰最早的 resolved。
        被淘汰者转为 evicted 档案保留在同一索引中（不再占容量），可继续按 id 追溯。
        """
        while self._active_count() >= self._capacity:
            victim = self._pick_eviction_victim()
            if victim is None:
                # 容量至少为 1 且在区已满时必有在区记录，正常不可达；防御性保留
                raise QuarantineFullError(
                    f"待处理区容量 {self._capacity} 已满，且没有可淘汰的记录"
                )
            at = self._clock()
            victim.status = PendingStatus.EVICTED
            victim.updated_at = at
            victim.evicted_at = at
            # 淘汰档案不再保留原始报文：既不可外泄，也不再可能被重放
            victim.payload = ""
            victim.history.append(
                HistoryEvent(
                    at=at,
                    action=LifecycleAction.EVICTED,
                    detail=(
                        f"待处理区达到容量上限 {self._capacity}，"
                        f"按最早进入顺序淘汰本记录（淘汰前状态/类别已保留）"
                    ),
                    reason=victim.reason.value,
                )
            )
            self._busy.discard(victim.pending_id)
            logger.info(
                "pending_id=%s 容量淘汰 类别=%s 进入时间=%s",
                victim.pending_id,
                victim.reason.value,
                victim.created_at,
            )

    def _active_count(self) -> int:
        return sum(1 for r in self._records.values() if r.status is not PendingStatus.EVICTED)

    def _pick_eviction_victim(self) -> PendingRecord | None:
        free_pending = [
            r for r in self._records.values()
            if r.status is PendingStatus.PENDING and r.pending_id not in self._busy
        ]
        if free_pending:
            return min(free_pending, key=lambda r: (r.created_at, self._order(r)))
        any_pending = [
            r for r in self._records.values() if r.status is PendingStatus.PENDING
        ]
        if any_pending:
            return min(any_pending, key=lambda r: (r.created_at, self._order(r)))
        resolved = [
            r for r in self._records.values() if r.status is PendingStatus.RESOLVED
        ]
        if resolved:
            return min(resolved, key=lambda r: (r.created_at, self._order(r)))
        return None

    # 进入顺序需要稳定可复现：用 created_at 排序，进入序号用于同时刻决胜
    def _order(self, record: PendingRecord) -> int:
        return record.seq

    # -- 查阅 ---------------------------------------------------------------
    def get(self, pending_id: str) -> PendingRecord | None:
        with self._lock:
            return self._records.get(pending_id)

    def list(
        self,
        *,
        status: PendingStatus | None = None,
        reason: RejectReason | None = None,
        start: str | None = None,
        end: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """按类别/时间范围过滤并分页；返回 items(安全视图)、total 与分类统计。

        默认（status=None）只返回**在区**记录（pending/resolved）；淘汰档案需显式
        指定 ``status=evicted`` 查阅，避免淘汰记录淹没日常待处理列表。
        """
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        with self._lock:
            rows = [
                r for r in self._records.values()
                if self._matches(r, status, reason, start, end)
            ]
            rows.sort(key=lambda r: (r.created_at, self._order(r)))
            total = len(rows)
            by_status: dict[str, int] = {}
            by_reason: dict[str, int] = {}
            for r in rows:
                by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
                by_reason[r.reason.value] = by_reason.get(r.reason.value, 0) + 1
            start_idx = (page - 1) * page_size
            page_rows = rows[start_idx : start_idx + page_size]
            return {
                "items": [r.to_view() for r in page_rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": (total + page_size - 1) // page_size,
                "counts": {"by_status": by_status, "by_reason": by_reason},
            }

    @staticmethod
    def _matches(
        r: PendingRecord,
        status: PendingStatus | None,
        reason: RejectReason | None,
        start: str | None,
        end: str | None,
    ) -> bool:
        # 默认只看在区记录；显式给状态（含 evicted）时按状态精确匹配
        if status is None:
            if r.status is PendingStatus.EVICTED:
                return False
        elif r.status is not status:
            return False
        if reason is not None and r.reason is not reason:
            return False
        if start is not None and r.created_at < start:
            return False
        if end is not None and r.created_at > end:
            return False
        return True

    # -- 重放并发控制与状态流转 ---------------------------------------------
    def acquire(self, pending_id: str) -> tuple[str, PendingRecord | None]:
        """尝试占用记录进行重放。

        返回 (状态, 记录)：
        - ("acquired", record)：占用成功，调用方最终必须 resolve/refail/release；
        - ("already_resolved", record)：记录此前已解决（幂等命中，不重复处理）；
        - ("busy", record)：同一刻已有重放在进行（并发保护）；
        - ("evicted", record)：记录已因容量淘汰转为档案，不能再重放；
        - ("not_found", None)：待处理区中无此记录。
        """
        with self._lock:
            record = self._records.get(pending_id)
            if record is None:
                return "not_found", None
            if record.status is PendingStatus.EVICTED:
                return "evicted", record
            if record.status is PendingStatus.RESOLVED:
                return "already_resolved", record
            if pending_id in self._busy:
                return "busy", record
            self._busy.add(pending_id)
            return "acquired", record

    def release(self, pending_id: str) -> None:
        with self._lock:
            self._busy.discard(pending_id)

    def resolve(self, pending_id: str, at: str, ingest_outcome: str) -> None:
        """重放通过：置为 resolved 并追加历史。"""
        with self._lock:
            record = self._records[pending_id]
            if record.status is PendingStatus.EVICTED:
                # 极端竞态：重放进行中记录被容量淘汰。入库已发生，结论记入档案，
                # 但不复活记录、不改其淘汰状态与失败类别。
                record.history.append(
                    HistoryEvent(
                        at=at,
                        action=LifecycleAction.REPLAYED,
                        detail=(
                            f"重放通过并已入库（{ingest_outcome}），但记录此前已被容量淘汰，"
                            "结论仅追加到档案，不恢复为在区记录"
                        ),
                        reason=None,
                        ingest_outcome=ingest_outcome,
                    )
                )
                self._busy.discard(pending_id)
                logger.warning(
                    "pending_id=%s 重放通过但记录已在重放期间被淘汰，结论仅存档",
                    pending_id,
                )
                return
            record.status = PendingStatus.RESOLVED
            record.updated_at = at
            record.resolved_at = at
            record.replay_count += 1
            record.history.append(
                HistoryEvent(
                    at=at,
                    action=LifecycleAction.REPLAYED,
                    detail=f"重放通过，按最新字典判定后入库（{ingest_outcome}）",
                    reason=None,
                    ingest_outcome=ingest_outcome,
                )
            )
            self._busy.discard(pending_id)

    def refail(self, pending_id: str, at: str, reason: RejectReason, detail: str,
               source_fragment: str, masked_payload: str) -> None:
        """重放仍失败：更新为最新失败类别/依据并追加历史，状态保持 pending。"""
        with self._lock:
            record = self._records[pending_id]
            if record.status is PendingStatus.EVICTED:
                # 同 resolve 的极端竞态：保留淘汰档案原貌，仅追加一次尝试结论
                record.history.append(
                    HistoryEvent(
                        at=at,
                        action=LifecycleAction.REPLAYED,
                        detail=(
                            f"重放仍失败（{detail}），但记录此前已被容量淘汰，"
                            "结论仅追加到档案，不恢复为在区记录"
                        ),
                        reason=reason.value,
                    )
                )
                self._busy.discard(pending_id)
                logger.warning(
                    "pending_id=%s 重放仍失败但记录已在重放期间被淘汰，结论仅存档",
                    pending_id,
                )
                return
            record.reason = reason
            record.detail = detail
            record.source_fragment = source_fragment
            record.masked_payload = masked_payload
            record.updated_at = at
            record.replay_count += 1
            record.history.append(
                HistoryEvent(
                    at=at,
                    action=LifecycleAction.REPLAYED,
                    detail=detail,
                    reason=reason.value,
                )
            )
            self._busy.discard(pending_id)

    def candidates(self, reason: RejectReason | None = None) -> list[PendingRecord]:
        """待重放候选：所有 pending 记录（可按类别限定），按进入顺序排列。"""
        with self._lock:
            rows = [
                r for r in self._records.values()
                if r.status is PendingStatus.PENDING
                and (reason is None or r.reason is reason)
            ]
            rows.sort(key=lambda r: (r.created_at, self._order(r)))
            return list(rows)

    def in_zone(self, reason: RejectReason | None = None) -> list[PendingRecord]:
        """批量重放范围：全部**在区**记录（pending 与 resolved，可按类别限定）。

        快照按进入顺序排列；resolved 者在本轮会得到 already_resolved 结论。
        evicted 档案不在范围内（无法重放）。
        """
        with self._lock:
            rows = [
                r for r in self._records.values()
                if r.status is not PendingStatus.EVICTED
                and (reason is None or r.reason is reason)
            ]
            rows.sort(key=lambda r: (r.created_at, self._order(r)))
            return list(rows)

    # -- 容量与统计 ----------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            by_reason: dict[str, int] = {}
            pending = resolved = evicted = 0
            for r in self._records.values():
                if r.status is PendingStatus.EVICTED:
                    evicted += 1
                    continue
                by_reason[r.reason.value] = by_reason.get(r.reason.value, 0) + 1
                if r.status is PendingStatus.PENDING:
                    pending += 1
                else:
                    resolved += 1
            return {
                "capacity": self._capacity,
                "total": self._active_count(),
                "pending": pending,
                "resolved": resolved,
                "evicted": evicted,
                "by_reason": by_reason,
            }
