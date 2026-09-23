"""检验项目编码注册表（LOINC 子集）与编码归一规则。

兼容规则：
- 大小写不敏感，允许首尾空白；
- 支持少量常见别名（如 "GLU" -> "2345-7"）；
- 不在注册表内的编码一律视为无法识别。
"""
from __future__ import annotations

# code -> (显示名, 默认单位)
KNOWN_CODES: dict[str, tuple[str, str]] = {
    "2345-7": ("Glucose [Mass/volume] in Serum or Plasma", "mg/dL"),
    "718-7": ("Hemoglobin [Mass/volume] in Blood", "g/dL"),
    "2093-3": ("Cholesterol [Mass/volume] in Serum or Plasma", "mg/dL"),
    "8480-6": ("Systolic blood pressure", "mm[Hg]"),
    "8462-4": ("Diastolic blood pressure", "mm[Hg]"),
    "8867-4": ("Heart rate", "/min"),
}

# 常见别名 -> 标准 LOINC 编码
_ALIASES: dict[str, str] = {
    "GLU": "2345-7",
    "GLUCOSE": "2345-7",
    "HGB": "718-7",
    "HB": "718-7",
    "CHOL": "2093-3",
    "SBP": "8480-6",
    "DBP": "8462-4",
    "HR": "8867-4",
}


def normalize_code(raw: str) -> str | None:
    """归一原始编码；无法识别时返回 None。"""
    if not raw:
        return None
    code = raw.strip().upper()
    code = _ALIASES.get(code, code)
    if code in KNOWN_CODES:
        return code
    return None
