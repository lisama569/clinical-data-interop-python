"""检验项目编码目录：来源编码识别、归一与单位线性换算。

- 规范编码（canonical）为内部统一编码；支持 LOINC 风格数字编码与各来源本地编码别名；
- 来源编码大小写/分隔符容错（``LOINC:2345-7`` 与 ``loinc2345-7`` 视为同一编码）；
- 单位按线性公式 ``canonical = value * factor + offset`` 换算到规范单位；
- 编码无法识别抛 :class:`RecordRejected`（UNKNOWN_CODE）；
  单位不在该编码允许集合内抛 UNIT_MISMATCH。

支持目录见 docs/compatibility.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.errors import RecordRejected
from app.models import RejectReason


@dataclass(frozen=True)
class CatalogEntry:
    canonical_code: str
    display: str
    canonical_unit: str
    aliases: frozenset[str]  # 可接受的来源编码（已规范化）
    # 已规范化单位 -> (factor, offset)，canonical = value * factor + offset
    units: dict[str, tuple[float, float]] = field(default_factory=dict)


def _norm_code(code: str) -> str:
    return "".join(ch for ch in code.strip().lower() if ch.isalnum())


def _norm_unit(unit: str) -> str:
    text = unit.strip().replace("μ", "u").replace("µ", "u")
    text = text.replace("＊", "*").replace("＾", "^")
    return text.replace(" ", "").lower()


# 单位拼写别名（在各编码换算表之外的通用写法归一）
_UNIT_SYNONYMS: dict[str, str] = {
    "mg/dl": "mg/dl",
    "mg%": "mg/dl",
    "g/dl": "g/dl",
    "g/l": "g/l",
    "mmol/l": "mmol/l",
    "umol/l": "umol/l",
    "m_eq/l": "meq/l",
    "meq/l": "meq/l",
    "10^9/l": "10^9/l",
    "10*9/l": "10^9/l",
    "giga/l": "10^9/l",
    "10^3/ul": "10^3/ul",
    "k/ul": "10^3/ul",
    "/ul": "/ul",
}

_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        canonical_code="GLU",
        display="Glucose [Mass/volume] in Blood",
        canonical_unit="mmol/L",
        aliases=frozenset({"loinc23457", "loinc23390", "localglu", "localglucose", "glu", "glucose"}),
        units={"mmol/l": (1.0, 0.0), "mg/dl": (1.0 / 18.0156, 0.0)},
    ),
    CatalogEntry(
        canonical_code="HGB",
        display="Hemoglobin [Mass/volume] in Blood",
        canonical_unit="g/L",
        aliases=frozenset({"loinc7187", "loinc205708", "localhgb", "hgb", "hemoglobin"}),
        units={"g/l": (1.0, 0.0), "g/dl": (10.0, 0.0)},
    ),
    CatalogEntry(
        canonical_code="CREA",
        display="Creatinine [Mass/volume] in Serum or Plasma",
        canonical_unit="umol/L",
        aliases=frozenset({"loinc21600", "loinc38483", "localcrea", "crea", "creatinine"}),
        units={"umol/l": (1.0, 0.0), "mg/dl": (88.4, 0.0)},
    ),
    CatalogEntry(
        canonical_code="WBC",
        display="Leukocytes [#/volume] in Blood",
        canonical_unit="10^9/L",
        aliases=frozenset({"loinc66902", "loinc264648", "localwbc", "wbc", "leukocytes"}),
        units={"10^9/l": (1.0, 0.0), "10^3/ul": (1.0, 0.0), "/ul": (0.001, 0.0)},
    ),
    CatalogEntry(
        canonical_code="PLT",
        display="Platelets [#/volume] in Blood",
        canonical_unit="10^9/L",
        aliases=frozenset({"loinc7773", "loinc265157", "localplt", "plt", "platelets"}),
        units={"10^9/l": (1.0, 0.0), "10^3/ul": (1.0, 0.0), "/ul": (0.001, 0.0)},
    ),
    CatalogEntry(
        canonical_code="K",
        display="Potassium [Moles/volume] in Serum or Plasma",
        canonical_unit="mmol/L",
        aliases=frozenset({"loinc28233", "loinc62984", "localk", "k", "potassium"}),
        units={"mmol/l": (1.0, 0.0), "meq/l": (1.0, 0.0)},
    ),
)

# 规范化来源编码 -> 目录项
_ALIAS_INDEX: dict[str, CatalogEntry] = {
    alias: entry for entry in _CATALOG for alias in entry.aliases
}


def _lookup(code: str) -> CatalogEntry | None:
    norm_code = _norm_code(code)
    entry = _ALIAS_INDEX.get(norm_code)
    if entry is None and norm_code.isdigit():
        # 纯数字编码视为隐式 LOINC 码（如 "2345-7" -> loinc23457）
        entry = _ALIAS_INDEX.get(f"loinc{norm_code}")
    return entry


def is_known(code: str) -> bool:
    try:
        return _lookup(code) is not None
    except AttributeError:
        return False


def _resolve_unit(unit: str) -> str:
    normalized = _norm_unit(unit)
    return _UNIT_SYNONYMS.get(normalized, normalized)


def canonicalize(code: str, value: float, unit: str) -> tuple[str, float, str]:
    """将来源编码与单位归一为 ``(规范编码, 规范数值, 规范单位)``。"""

    try:
        entry = _lookup(code)
    except AttributeError as exc:  # pragma: no cover - 防御性
        raise RecordRejected(RejectReason.MISSING_FIELD, "observation code is absent") from exc
    if entry is None:
        raise RecordRejected(RejectReason.UNKNOWN_CODE, f"unrecognized observation code: {code!r}")
    norm_unit = _resolve_unit(unit)
    conversion = entry.units.get(norm_unit)
    if conversion is None:
        raise RecordRejected(
            RejectReason.UNIT_MISMATCH,
            f"unit {unit!r} is incompatible with code {entry.canonical_code}",
        )
    factor, offset = conversion
    canonical_value = value * factor + offset
    return entry.canonical_code, canonical_value, entry.canonical_unit


def display_name(canonical_code: str) -> str:
    """返回规范编码的展示名（供文档/导出使用）。"""

    for entry in _CATALOG:
        if entry.canonical_code == canonical_code:
            return entry.display
    raise KeyError(canonical_code)
