"""被拒记录待处理区（quarantine）：持久化、分页查阅、生命周期与容量淘汰。

设计要点：
- 因 malformed_format / missing_required_field / unknown_code 被拒的记录按失败类别进入待处理区；
- 待处理条目有明确状态（pending/failed/resolved）与追加式处理历史，可追溯何时因何被拒、
  何时被重放、结果如何；被淘汰条目转为 evicted 墓碑（丢弃原始报文），不占容量但保留审计轨迹；
- 原始报文仅以私有字段保存在进程内存（永不进入对外视图），对外只暴露脱敏片段与脱敏后的依据；
- 容量有可配置上限（只计 pending/failed），达到上限时按进入顺序（FIFO，entry 序号升序）淘汰
  最老的未解决条目；已解决记录永不淘汰，淘汰也不改变任何既有记录的失败类别（类别保存在历史中）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from app.masking import mask_free_text
from app.models import RejectReason


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuarantineStatus(str, Enum):
    PENDING = "pending"      # 待处理：等待字典更新或人工修复后重放
    FAILED = "failed"        # 重放后仍不通过，失败类别更新为最新结论，继续留在待处理区
    RESOLVED = "resolved"    # 已解决：重放通过并完成入库
    EVICTED = "evicted"      # 已被容量淘汰（墓碑，仅保留审计轨迹，不可再重放）


class ReplayOutcome(str, Enum):
    RESOLVED = "resolved"          # 本次重放判定通过并完成入库
    STILL_FAILED = "still_failed"  # 本次重放仍不通过，失败类别/依据已更新为最新结论
    ALREADY_RESOLVED = "already_resolved"  # 幂等命中：条目已解决，不重复处理
    NOT_PROCESSED = "not_processed"        # 批量重放中未被选中（筛选条件不匹配）
    NOT_FOUND = "not_found"                # 条目不存在或已被淘汰


@dataclass(frozen=True)
class QuarantineEvent:
    """生命周期中的一次状态变化（追加式、不可变）。"""

    at: str                 # ISO8601 UTC 时间戳
    action: str             # admitted / replayed / evicted
    from_status: str | None
    to_status: str | None
    reason: str | None = None       # 当时的失败类别（RejectReason 值）
    detail: str | None = None       # 脱敏后的依据
    result: str | None = None       # 重放的记录级结论（created/duplicate/conflict）


@dataclass
class QuarantineEntry:
    """待处理区中的一条被拒记录（对外视图全部为脱敏内容）。"""

    entry_id: str
    source_id: str
    batch_id: str
    reason: RejectReason
    detail: str                       # 脱敏后的判定依据
    source_fragment: str              # 脱敏后的报文片段
    code_token: str | None
    status: QuarantineStatus
    created_at: str
    updated_at: str
    attempt_count: int = 0
    history: list[QuarantineEvent] = field(default_factory=list)
    # 原始报文仅用于重放时重新解析；永不进入任何对外序列化视图
    _payload_raw: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class QuarantinePage:
    """分页查阅结果与分页统计。"""

    items: list[QuarantineEntry]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class ReplayResult:
    """单条重放结论。"""

    entry_id: str
    outcome: ReplayOutcome
    record_outcome: str | None = None  # resolved 时的入库结论 created/duplicate/conflict
    reason: str | None = None          # still_failed 时最新失败类别
    detail: str | None = None          # 脱敏后的最新依据


class QuarantineStore:
    """线程安全的待处理区存储。"""

    def __init__(self, capacity: int = 1000, clock=_utcnow) -> None:
        if capacity < 1:
            raise ValueError("capacity 必须 >= 1")
        self._lock = threading.RLock()
        self._capacity = capacity
        self._clock = clock
        self._seq = 0
        self._entries: dict[str, QuarantineEntry] = {}   # 按进入顺序保留
        self._tombstones: dict[str, QuarantineEntry] = {}  # 被淘汰条目的墓碑
        self._evicted_total = 0

    # ------------------------------------------------------------------ 内部工具

    def _view(self, entry: QuarantineEntry, *, include_payload: bool = False) -> QuarantineEntry:
        """返回条目副本；默认剥离原始报文，保证调用方拿不到敏感原文。"""
        return replace(
            entry,
            history=list(entry.history),
            _payload_raw=entry._payload_raw if include_payload else "",
        )

    def _occupancy(self) -> int:
        return sum(
            1 for e in self._entries.values()
            if e.status in (QuarantineStatus.PENDING, QuarantineStatus.FAILED)
        )

    def _evict_oldest(self, at: str) -> None:
        """容量已满时淘汰最老的未解决条目（FIFO：dict 插入顺序即 entry 序号升序）。"""
        # 调用方已持锁
        victim_id = next(
            (eid for eid, e in self._entries.items()
             if e.status in (QuarantineStatus.PENDING, QuarantineStatus.FAILED)),
            None,
        )
        if victim_id is None:
            return  # 全部已解决，不淘汰已解决记录
        victim = self._entries.pop(victim_id)
        victim.history.append(QuarantineEvent(
            at=at, action="evicted",
            from_status=victim.status.value, to_status=QuarantineStatus.EVICTED.value,
            reason=victim.reason.value, detail="容量上限触发 FIFO 淘汰",
        ))
        victim.status = QuarantineStatus.EVICTED
        victim.updated_at = at
        victim._payload_raw = ""  # 淘汰时丢弃原始报文
        self._tombstones[victim_id] = victim
        self._evicted_total += 1

    # ------------------------------------------------------------------ 写入

    def admit(self, *, batch_id: str, source_id: str, payload: str,
              reason: RejectReason, detail: str, source_fragment: str,
              code_token: str | None, salt: str, at: str | None = None) -> QuarantineEntry:
        """把一条被拒记录收入待处理区；容量不足时先按 FIFO 淘汰。"""
        ts = at or self._clock()
        # 入口再脱敏一次：依据与片段都不允许出现直接标识原文
        safe_detail = mask_free_text(detail, salt)
        safe_fragment = mask_free_text(source_fragment, salt)
        with self._lock:
            if self._occupancy() >= self._capacity:
                self._evict_oldest(ts)
            self._seq += 1
            entry_id = f"Q{self._seq:08d}"
            entry = QuarantineEntry(
                entry_id=entry_id,
                source_id=source_id,
                batch_id=batch_id,
                reason=reason,
                detail=safe_detail,
                source_fragment=safe_fragment,
                code_token=(code_token or None),
                status=QuarantineStatus.PENDING,
                created_at=ts,
                updated_at=ts,
                history=[QuarantineEvent(
                    at=ts, action="admitted", from_status=None,
                    to_status=QuarantineStatus.PENDING.value,
                    reason=reason.value, detail=safe_detail,
                )],
                _payload_raw=payload,
            )
            self._entries[entry_id] = entry
            return self._view(entry)

    def begin_replay(self, entry_id: str) -> QuarantineEntry | None:
        """取出可重放条目（含原始报文，仅供服务层重新解析）。

        - pending/failed：返回带原始报文的副本并把尝试计数 +1；
        - resolved：返回普通视图（无原始报文），由服务层判定为幂等命中；
        - 不存在或已淘汰（墓碑）：返回 None。
        """
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            if entry.status in (QuarantineStatus.PENDING, QuarantineStatus.FAILED):
                entry.attempt_count += 1
                return self._view(entry, include_payload=True)
            return self._view(entry)

    def finish_replay(self, entry_id: str, *, resolved: bool,
                      record_outcome: str | None = None,
                      reason: RejectReason | None = None,
                      detail: str | None = None, salt: str = "",
                      at: str | None = None) -> None:
        """落定一次重放的最新结论；并发由服务层重放总锁串行化。"""
        ts = at or self._clock()
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None or entry.status is QuarantineStatus.RESOLVED:
                # 已解决结论受保护：后续触发不得覆盖
                return
            before = entry.status.value
            if resolved:
                entry.status = QuarantineStatus.RESOLVED
                event_to = QuarantineStatus.RESOLVED.value
                event_reason = None
                event_detail = None
            else:
                # 仍失败：失败类别与依据更新为最新结论，继续留在待处理区
                if reason is not None:
                    entry.reason = reason
                if detail is not None:
                    entry.detail = mask_free_text(detail, salt)
                entry.status = QuarantineStatus.FAILED
                event_to = QuarantineStatus.FAILED.value
                event_reason = entry.reason.value
                event_detail = entry.detail
            entry.updated_at = ts
            entry.history.append(QuarantineEvent(
                at=ts, action="replayed", from_status=before, to_status=event_to,
                reason=event_reason, detail=event_detail, result=record_outcome,
            ))

    # ------------------------------------------------------------------ 读取

    def get(self, entry_id: str) -> QuarantineEntry | None:
        """对外查阅：返回脱敏视图（含墓碑）。原始报文永不出现在视图中。"""
        with self._lock:
            entry = self._entries.get(entry_id) or self._tombstones.get(entry_id)
            return self._view(entry) if entry is not None else None

    def list_page(self, *, reason: RejectReason | None = None,
                  status: QuarantineStatus | None = None,
                  start: str | None = None, end: str | None = None,
                  limit: int = 50, offset: int = 0) -> QuarantinePage:
        """按失败类别、状态、进入时间范围分页；按进入顺序（entry_id 升序）稳定排序。"""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        with self._lock:
            matched = [
                e for e in self._entries.values()
                if (reason is None or e.reason is reason)
                and (status is None or e.status is status)
                and (start is None or e.created_at >= start)
                and (end is None or e.created_at <= end)
            ]
            matched.sort(key=lambda e: e.entry_id)
            page = [self._view(e) for e in matched[offset:offset + limit]]
            return QuarantinePage(items=page, total=len(matched), limit=limit, offset=offset)

    def list_actionable(self, *, reason: RejectReason | None = None,
                        code_token: str | None = None) -> list[QuarantineEntry]:
        """列出仍可重放（pending/failed）的条目，供批量重放筛选（按进入顺序）。"""
        token = code_token.strip().upper() if code_token else None
        with self._lock:
            out = []
            for e in self._entries.values():
                if e.status not in (QuarantineStatus.PENDING, QuarantineStatus.FAILED):
                    continue
                if reason is not None and e.reason is not reason:
                    continue
                if token is not None and e.code_token != token:
                    continue
                out.append(self._view(e))
            return out

    def counts(self) -> dict[str, int]:
        """分页/容量统计：按状态、按失败类别计数（不含任何报文内容）。"""
        with self._lock:
            by_status = {s.value: 0 for s in QuarantineStatus}
            by_reason = {r.value: 0 for r in RejectReason}
            for e in self._entries.values():
                by_status[e.status.value] += 1
                by_reason[e.reason.value] += 1
            return {
                "capacity": self._capacity,
                "occupancy": self._occupancy(),
                "total": len(self._entries),
                **by_status,
                **{f"reason:{k}": v for k, v in by_reason.items()},
                "evicted_total": self._evicted_total,
            }
