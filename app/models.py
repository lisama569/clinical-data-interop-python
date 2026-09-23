"""统一领域模型与错误/冲突原因枚举。

两种交换格式（简化 FHIR JSON、CSV）均归一为本模块定义的
患者（Patient）/就诊（Encounter）/观测（Observation）模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Format(str, Enum):
    """支持的临床数据交换格式。"""

    FHIR_JSON = "fhir-json"
    CSV = "csv"


class RejectReason(str, Enum):
    """记录级拒绝原因（须可区分，不得静默丢弃）。"""

    MALFORMED = "MALFORMED"  # 语法/结构非法，无法解析
    MISSING_FIELD = "MISSING_FIELD"  # 必填字段缺失
    UNKNOWN_CODE = "UNKNOWN_CODE"  # 检验项目编码无法识别
    UNIT_MISMATCH = "UNIT_MISMATCH"  # 单位与编码不兼容
    INVALID_VALUE = "INVALID_VALUE"  # 数值/日期等字段格式非法
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"  # 格式/版本不支持
    DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"  # 同一批次内重复行


class IngestStatus(str, Enum):
    """批次提交状态。"""

    ACCEPTED = "ACCEPTED"  # 全部条目处理完成（可能含拒绝）
    REPLAYED = "REPLAYED"  # 相同批次幂等重放，未产生任何新写入
    PARTIAL = "PARTIAL"  # 部分接受、部分拒绝


class ConflictVerdict(str, Enum):
    """并发/重复更新同一资源时的冲突结论（须一致且可区分）。"""

    NO_CONFLICT = "NO_CONFLICT"  # 无冲突，已应用
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"  # 与现有内容一致，空操作
    VERSION_CONFLICT = "VERSION_CONFLICT"  # 版本过期，拒绝覆盖
    DIVERGENT_CONTENT = "DIVERGENT_CONTENT"  # 同版本但内容不同，拒绝覆盖


class ResourceType(str, Enum):
    PATIENT = "Patient"
    ENCOUNTER = "Encounter"
    OBSERVATION = "Observation"


class Role(str, Enum):
    """访问控制角色。"""

    INGEST = "ingest"  # 仅可提交导入
    READER = "reader"  # 脱敏明细查询
    EXPORTER = "exporter"  # 脱敏批量导出
    AUDITOR = "auditor"  # 可查拒绝原因与原始片段（仍脱敏）
    ADMIN = "admin"  # 更新资源
    ANONYMOUS = "anonymous"  # 无任何业务权限


@dataclass(frozen=True)
class Patient:
    """归一后的患者。直接标识不以明文进入任何读取路径。"""

    source_id: str  # 来源系统中的患者业务标识
    source_system: str  # 来源系统标识
    name_token: str  # 姓名令牌（HMAC，用于匹配）
    ident_token: str  # 证件号令牌（HMAC，用于匹配）
    contact_token: str  # 联系方式令牌（HMAC，用于匹配）
    birth_date: str | None = None  # ISO yyyy-mm-dd
    gender: str | None = None  # male/female/other/unknown
    version: int = 1


@dataclass(frozen=True)
class Encounter:
    """归一后的就诊记录。"""

    source_id: str
    source_system: str
    patient_source_id: str
    status: str  # planned/in-progress/finished/cancelled
    start_time: str | None = None  # ISO8601
    end_time: str | None = None
    version: int = 1


@dataclass(frozen=True)
class Observation:
    """归一后的检验观测。

    数值统一换算为 canonical_code 对应的 canonical_unit。
    """

    source_id: str
    source_system: str
    patient_source_id: str
    encounter_source_id: str | None
    canonical_code: str  # 归一后的检验项目编码
    canonical_value: float
    canonical_unit: str
    effective_time: str  # ISO8601
    status: str = "final"  # registered/preliminary/final/amended
    version: int = 1


@dataclass(frozen=True)
class RawEntry:
    """待解析的单条原始记录及其定位信息。"""

    index: int  # 在批次内的序号（从 0 开始）
    locator: str  # 可定位描述，如 Bundle.entry[3] 或 csv:row=4
    raw: str  # 原始片段文本


@dataclass(frozen=True)
class Rejection:
    """被拒绝记录的结构化结论（不含原始直接标识明文）。"""

    index: int
    locator: str
    reason: RejectReason
    detail: str  # 判定依据，仅含字段名/编码等非敏感信息
    masked_preview: str  # 已脱敏的片段预览
    resource_type: ResourceType | None = None


@dataclass(frozen=True)
class ParseReport:
    """单批次解析结果。"""

    patients: list[Patient] = field(default_factory=list)
    encounters: list[Encounter] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


@dataclass(frozen=True)
class BatchResult:
    """批次导入结论。"""

    batch_id: str
    status: IngestStatus
    accepted_counts: dict[str, int]
    rejected_count: int
    replay: bool  # 是否为幂等重放
    conflicts: tuple[str, ...] = ()  # 冲突条目摘要（locator + verdict）
    rejections: tuple[Rejection, ...] = ()
