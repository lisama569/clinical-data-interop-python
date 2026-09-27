"""被拒记录待处理区：分类持久化、分页查阅、容量淘汰与重放并发控制。

设计要点：
- 进入：被拒记录按失败类别（malformed_format / missing_required_field / unknown_code）
  持久化，保留脱敏上下文（source_fragment / masked_payload）；原始报文仅存于
  ``payload`` 字段供重放，任何对外视图都不得包含它。
- 退出：重放判定通过 -> resolved（记录仍保留用于追溯）；仍失败 -> 保持 pending 并
  更新为最新失败类别与依据。
- 容量：容量只约束活动区（pending + resolved）。容量满时按确定顺序淘汰
  （created_at 升序、序号决胜）：优先淘汰最早的 pending；若活动区全是 resolved，
  则淘汰最早的 resolved。因此**任何新的被拒记录都必定能进入待处理区**，不会被
  静默丢弃。被淘汰记录转入可追溯归档（status=evicted）：保留失败类别与完整处理
  历史，可通过列表/明细查询，但删除仅供重放的原始报文。淘汰绝不改变其余记录的
  失败类别。
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

# 默认待处理区容量上限（仅约束活动区：pending 与 resolved）
DEFAULT_CAPACITY = 1000


class QuarantineFullError(Exception):
    """容量已满且无法为新记录腾出位置（正常流程不会发生：淘汰总会腾出位置）。

    保留该异常类型仅为向后兼容；当前容量策略保证新的被拒记录总能入区。
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
        # 容量淘汰归档：evicted 记录仍可查阅（保留类别与历史），但不占容量、不可重放
        self._archive: dict[str, PendingRecord] = {}
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

        返回 (记录, 是否新建)。容量不足时按规则淘汰一条活动记录腾出位置，
        因此被拒记录不会因容量而丢失。
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
        """容量满时按确定顺序淘汰活动记录，为新记录腾出一个位置。

        优先淘汰最早的 pending；活动区全是 resolved 时淘汰最早的 resolved。
        被淘汰记录转为 evicted 进入归档（保留失败类别与处理历史、清除原始报文），
        其余活动记录的任何字段（尤其失败类别）都不会被改动。
        """
        while len(self._records) >= self._capacity:
            victim = min(
                self._records.values(),
                key=lambda r: (
                    # pending 优先于 resolved 被淘汰
                    0 if r.status is PendingStatus.PENDING else 1,
                    r.created_at,
                    self._order(r),
                ),
            )
            self._evict(victim)

    def _evict(self, victim: PendingRecord) -> None:
        at = self._clock()
        victim.status = PendingStatus.EVICTED
        victim.evicted_at = at
        victim.updated_at = at
        # 原始报文仅供重放，淘汰后不可再重放，故清除以免长期留存敏感原文
        victim.payload = ""
        victim.history.append(
            HistoryEvent(
                at=at,
                action=LifecycleAction.EVICTED,
                detail=(
                    "容量上限触发淘汰：记录转入可追溯归档，保留失败类别与处理历史；"
                    "归档记录不可再重放"
                ),
                reason=victim.reason.value,
            )
        )
        self._busy.discard(victim.pending_id)
        del self._records[victim.pending_id]
        self._archive[victim.pending_id] = victim
        logger.info(
            "pending_id=%s 容量淘汰 淘汰时类别=%s 进入时间=%s",
            victim.pending_id,
            victim.reason.value,
            victim.created_at,
        )

    # 进入顺序需要稳定可复现：用 created_at 排序，进入序号用于同时刻决胜
    def _order(self, record: PendingRecord) -> int:
        return record.seq

    # -- 查阅 ---------------------------------------------------------------
    def get(self, pending_id: str) -> PendingRecord | None:
        """按 id 查阅，活动区与淘汰归档均可命中（归档记录 status=evicted）。"""
        with self._lock:
            return self._records.get(pending_id) or self._archive.get(pending_id)

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

        活动记录与已淘汰归档记录统一可见（可用 ``status=evicted`` 只看归档）。
        """
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        with self._lock:
            all_rows = list(self._records.values()) + list(self._archive.values())
            rows = [r for r in all_rows if self._matches(r, status, reason, start, end)]
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
        if status is not None and r.status is not status:
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
        - ("evicted", record)：记录已被容量淘汰归档，不可再重放（仍可查阅）；
        - ("not_found", None)：活动区与归档中均无此记录。
        """
        with self._lock:
            record = self._records.get(pending_id)
            if record is None:
                archived = self._archive.get(pending_id)
                if archived is not None:
                    return "evicted", archived
                return "not_found", None
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

    def replay_targets(
        self, reason: RejectReason | None = None
    ) -> list[PendingRecord]:
        """批量重放候选：pending 记录（可按类别限定）+ 全部 resolved 记录。

        pending 排在前面（按进入顺序），resolved 随后（按解决前进入顺序）；
        这样批量重放既能处理“仍失败的”，也能对“此前已解决”的给出
        already_resolved 结论，而不把它们误算成本轮解决。淘汰归档不参与。
        """
        with self._lock:
            pending_rows = [
                r for r in self._records.values()
                if r.status is PendingStatus.PENDING
                and (reason is None or r.reason is reason)
            ]
            resolved_rows = [
                r for r in self._records.values()
                if r.status is PendingStatus.RESOLVED
            ]
            pending_rows.sort(key=lambda r: (r.created_at, self._order(r)))
            resolved_rows.sort(key=lambda r: (r.created_at, self._order(r)))
            return pending_rows + resolved_rows

    # -- 容量与统计 ----------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            by_reason: dict[str, int] = {}
            pending = resolved = evicted = 0
            for r in self._records.values():
                by_reason[r.reason.value] = by_reason.get(r.reason.value, 0) + 1
                if r.status is PendingStatus.PENDING:
                    pending += 1
                else:
                    resolved += 1
            for r in self._archive.values():
                evicted += 1
                by_reason[r.reason.value] = by_reason.get(r.reason.value, 0) + 1
            return {
                "capacity": self._capacity,
                "total": len(self._records),
                "pending": pending,
                "resolved": resolved,
                "evicted": evicted,
                "by_reason": by_reason,
            }
