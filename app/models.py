"""统一数据模型：患者、就诊、观测记录，以及摄取结果类型。"""
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
    code_token: str | None = None  # 无法识别的原始编码令牌（仅 unknown_code，便于字典更新后定向重放）


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


@dataclass
class BatchResult:
    batch_id: str
    replayed: bool                 # 该批次是否命中幂等重放
    results: list[RecordResult] = field(default_factory=list)
