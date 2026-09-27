"""检验项目编码注册表（LOINC 子集）与编码归一规则。

兼容规则：
- 大小写不敏感，允许首尾空白；
- 支持少量常见别名（如 "GLU" -> "2345-7"）；
- 不在注册表内的编码一律视为无法识别。

运行时扩展：
- register_code / register_alias 可在不重启服务的前提下新增编码或别名，注册即时生效、线程安全；
- 重复注册已存在的编码/别名且归一结论一致时幂等（no-op）；冲突映射抛 ValueError，不静默改表；
- 注册只增不改既有映射，避免历史结论被悄悄翻转。
"""
from __future__ import annotations

import threading


class CodeRegistryConflictError(ValueError):
    """同一令牌已映射到不同标准编码，拒绝覆盖。"""


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
_DEFAULT_ALIASES: dict[str, str] = {
    "GLU": "2345-7",
    "GLUCOSE": "2345-7",
    "HGB": "718-7",
    "HB": "718-7",
    "CHOL": "2093-3",
    "SBP": "8480-6",
    "DBP": "8462-4",
    "HR": "8867-4",
}

_lock = threading.RLock()
_codes: dict[str, tuple[str, str]] = dict(KNOWN_CODES)
_aliases: dict[str, str] = dict(_DEFAULT_ALIASES)


def normalize_code(raw: str) -> str | None:
    """归一原始编码；无法识别时返回 None。读快照，保证与并发注册互不干扰。"""
    if not raw:
        return None
    token = raw.strip().upper()
    with _lock:
        code = _aliases.get(token, token)
        if code in _codes:
            return code
    return None


def register_code(code: str, display: str = "", unit: str = "") -> str:
    """运行时新增一个标准编码；已存在且等价时幂等。返回归一后的编码。"""
    if not code or not code.strip():
        raise ValueError("编码不能为空")
    normalized = code.strip().upper()
    with _lock:
        existing = _codes.get(normalized)
        if existing is not None:
            # 幂等：编码已在注册表即视为成功，不覆盖既有显示名/单位
            return normalized
        _codes[normalized] = (display or normalized, unit)
    return normalized


def register_alias(alias: str, code: str) -> str:
    """运行时新增别名 -> 标准编码；已存在同映射幂等，冲突抛错不覆盖。返回标准编码。"""
    if not alias or not alias.strip():
        raise ValueError("别名不能为空")
    token = alias.strip().upper()
    target = code.strip().upper()
    with _lock:
        if target not in _codes:
            raise ValueError(f"目标编码未注册: {target}，请先 register_code")
        existing = _aliases.get(token)
        if existing is not None:
            if existing == target:
                return target  # 幂等：同一别名重复映射到同一编码
            raise CodeRegistryConflictError(
                f"别名 {token} 已映射到 {existing}，拒绝覆盖为 {target}"
            )
        _aliases[token] = target
    return target


def known_codes_snapshot() -> dict[str, tuple[str, str]]:
    """返回当前注册表快照（测试/排查用）。"""
    with _lock:
        return dict(_codes)


def aliases_snapshot() -> dict[str, str]:
    """返回当前别名表快照（测试/排查用）。"""
    with _lock:
        return dict(_aliases)


def reset_registry() -> None:
    """恢复内置编码与别名（仅供测试隔离，生产路径不调用）。"""
    with _lock:
        _codes.clear()
        _codes.update(KNOWN_CODES)
        _aliases.clear()
        _aliases.update(_DEFAULT_ALIASES)
