"""统一数据模型：患者、就诊、观测记录、摄取结果，以及待处理区生命周期类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RejectReason(str, Enum):
    """记录被拒绝的原因分类。"""

    MALFORMED_FORMAT = "malformed_format"          # 格式非法，无法解析
    MISSING_REQUIRED_FIELD = "missing_required_field"  # 必填字段缺失
    UNKNOWN_CODE = "unknown_code"                  # 检验项目编码无法识别


@dataclass(frozen=True)
class Rejection:
    """一条被拒绝的记录及其可定位的原始片段。"""

    reason: RejectReason
    detail: str
    source_fragment: str  # 原始报文中可定位的片段（已脱敏）


@dataclass(frozen=True)
class Patient:
    patient_key: str          # 由源系统患者标识派生的稳定键
    masked_name: str          # 脱敏后的姓名
    masked_id_number: str     # 脱敏后的证件号
    masked_phone: str         # 脱敏后的联系方式


@dataclass(frozen=True)
class Encounter:
    encounter_key: str
    patient_key: str
    period_start: str
    encounter_type: str


@dataclass(frozen=True)
class Observation:
    observation_key: str
    encounter_key: str
    code: str                 # 标准化检验编码（LOINC 子集）
    value: str
    unit: str
    effective_time: str


@dataclass
class NormalizedBundle:
    """一条源记录归一化后的结果。"""

    patient: Patient | None = None
    encounter: Encounter | None = None
    observations: list[Observation] = field(default_factory=list)


class RecordOutcome(str, Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"        # 幂等命中：内容一致，未产生新记录
    CONFLICT = "conflict"          # 同一资源键并发/重复更新且内容不一致
    REJECTED = "rejected"


@dataclass(frozen=True)
class RecordResult:
    source_id: str
    outcome: RecordOutcome
    rejection: Rejection | None = None
    pending_id: str | None = None  # 被拒进入待处理区时的定位 id


@dataclass
class BatchResult:
    batch_id: str
    replayed: bool                 # 该批次是否命中幂等重放
    results: list[RecordResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 待处理区（quarantine）：被拒记录的持久化、重放与生命周期
# ---------------------------------------------------------------------------


class PendingStatus(str, Enum):
    """待处理记录的生命周期状态。"""

    PENDING = "pending"      # 仍在待处理区，尚未通过判定
    RESOLVED = "resolved"    # 重放后通过判定并完成入库
    EVICTED = "evicted"      # 容量上限触发淘汰，已离开待处理区但保留可追溯档案


class LifecycleAction(str, Enum):
    """处理历史中的动作类型。"""

    REJECTED = "rejected"      # 初次摄取被拒，进入待处理区
    REPLAYED = "replayed"      # 一次重放尝试（无论结果）
    EVICTED = "evicted"        # 容量上限触发淘汰；记录转为 evicted 档案，保留失败类别与完整历史


class ReplayOutcome(str, Enum):
    """单条重放的可区分结论。"""

    RESOLVED = "resolved"              # 本次重放通过判定并完成入库
    STILL_FAILED = "still_failed"      # 重放后仍不通过，继续留在待处理区
    ALREADY_RESOLVED = "already_resolved"  # 幂等命中：该记录此前已解决
    SKIPPED_BUSY = "skipped_busy"      # 并发安全：同一刻已有重放在进行
    NOT_FOUND = "not_found"            # 待处理区中不存在该记录
    EVICTED = "evicted"                # 因容量淘汰已离开待处理区（详情查询时）


@dataclass(frozen=True)
class HistoryEvent:
    """生命周期中的一次可追溯事件。所有字段均为脱敏后的安全信息。"""

    at: str                 # ISO8601 时间戳
    action: LifecycleAction
    detail: str             # 判定依据（如拒绝原因、入库 outcome），不含原始敏感值
    reason: str | None = None       # 当时的失败类别（RejectReason 值）
    ingest_outcome: str | None = None  # 重放通过时的记录级入库结论


@dataclass
class PendingRecord:
    """待处理区中的一条被拒记录。

    - ``source_fragment`` / ``masked_payload``：可对外展示的脱敏上下文；
    - ``payload``：仅用于重放的原始报文，标记为私有，禁止出现在任何视图、错误与日志中。
    """

    pending_id: str
    source_id: str                 # 来源定位（batch#idx）
    batch_id: str
    format: str                    # 探测到的格式（fhir_json/hl7v2/unknown）
    reason: RejectReason           # 最新失败类别
    detail: str                    # 最新判定依据
    source_fragment: str           # 脱敏片段（报文前 300 字符）
    masked_payload: str            # 完整报文的脱敏版本（查阅用上下文）
    payload: str                   # 原始报文（仅供重放，禁止外泄）
    fingerprint: str = ""          # 报文内容指纹（HMAC），用于同内容重复进入的幂等去重
    seq: int = 0                   # 进入序号（同时刻淘汰/排序的确定性决胜键）
    status: PendingStatus = PendingStatus.PENDING
    created_at: str = ""
    updated_at: str = ""
    resolved_at: str | None = None
    evicted_at: str | None = None
    replay_count: int = 0
    history: list[HistoryEvent] = field(default_factory=list)

    def to_view(self) -> dict:
        """对外可展示的安全视图（绝不含原始报文）。"""
        return {
            "pending_id": self.pending_id,
            "source_id": self.source_id,
            "batch_id": self.batch_id,
            "format": self.format,
            "status": self.status.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "source_fragment": self.source_fragment,
            "masked_payload": self.masked_payload,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "evicted_at": self.evicted_at,
            "replay_count": self.replay_count,
            "history": [
                {
                    "at": h.at,
                    "action": h.action.value,
                    "detail": h.detail,
                    "reason": h.reason,
                    "ingest_outcome": h.ingest_outcome,
                }
                for h in self.history
            ],
        }


@dataclass(frozen=True)
class ReplayResult:
    """一次单条重放的结果。"""

    pending_id: str
    outcome: ReplayOutcome
    reason: str | None = None            # still_failed 时的最新失败类别
    detail: str | None = None            # still_failed 时的最新依据
    ingest_outcome: str | None = None    # resolved 时的记录级入库结论


@dataclass
class ReplayBatchResult:
    """一次批量重放的汇总。

    ``triggered`` 为本轮触发时纳入范围的记录数（按类别过滤后的全部在区记录，
    含 pending 与已 resolved 者）；每条记录都有一条对应的 ``ReplayResult``。
    """

    triggered: int = 0                              # 本轮纳入范围的记录数
    results: list[ReplayResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """逐条原始结论计数（``ReplayOutcome`` 值 -> 条数）。"""
        c: dict[str, int] = {}
        for r in self.results:
            c[r.outcome.value] = c.get(r.outcome.value, 0) + 1
        return c

    def summary(self) -> dict[str, int]:
        """面向跟进的四类结论汇总：

        - ``resolved``：本轮重放新通过并入库；
        - ``still_failed``：本轮处理了但仍失败，继续留在待处理区；
        - ``already_resolved``：此前已解决，本轮属于重复触发，未重复处理；
        - ``not_reached``：本轮未实际处理到（并发占用跳过等），仍需后续跟进。

        四类之和恒等于 ``triggered``。
        """
        c = self.counts()
        not_reached = (
            c.get(ReplayOutcome.SKIPPED_BUSY.value, 0)
            + c.get(ReplayOutcome.NOT_FOUND.value, 0)
            + c.get(ReplayOutcome.EVICTED.value, 0)
        )
        return {
            ReplayOutcome.RESOLVED.value: c.get(ReplayOutcome.RESOLVED.value, 0),
            ReplayOutcome.STILL_FAILED.value: c.get(ReplayOutcome.STILL_FAILED.value, 0),
            ReplayOutcome.ALREADY_RESOLVED.value: c.get(
                ReplayOutcome.ALREADY_RESOLVED.value, 0
            ),
            "not_reached": not_reached,
        }
