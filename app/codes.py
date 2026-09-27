"""检验项目编码注册表（LOINC 子集）与编码归一规则。

兼容规则：
- 大小写不敏感，允许首尾空白；
- 支持少量常见别名（如 "GLU" -> "2345-7"）；
- 不在注册表内的编码一律视为无法识别。

运行时扩展（不重启服务）：
- ``CodeRegistry.register_code`` 新增（或修正）编码；
- ``CodeRegistry.register_alias`` 为已注册编码新增别名；
- 所有注册均在锁内完成，重放读到的总是一致的最新字典。
"""
from __future__ import annotations

import threading

# code -> (显示名, 默认单位)
_DEFAULT_CODES: dict[str, tuple[str, str]] = {
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


class UnknownTargetCodeError(ValueError):
    """别名试图指向一个尚未注册的编码。"""


class CodeRegistry:
    """可在运行时扩充的检验项目编码注册表。线程安全。"""

    def __init__(
        self,
        codes: dict[str, tuple[str, str]] | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._codes: dict[str, tuple[str, str]] = dict(codes or _DEFAULT_CODES)
        self._aliases: dict[str, str] = {
            k.upper(): v for k, v in (aliases or _DEFAULT_ALIASES).items()
        }

    def normalize_code(self, raw: str) -> str | None:
        """归一原始编码；无法识别时返回 None。总是读取最新字典。"""
        if not raw:
            return None
        with self._lock:
            code = raw.strip().upper()
            code = self._aliases.get(code, code)
            if code in self._codes:
                return code
            return None

    def register_code(self, code: str, display: str = "", unit: str = "") -> str:
        """新增编码；已存在时为幂等的元数据修正。返回规范化后的标准编码。"""
        normalized = (code or "").strip().upper()
        if not normalized:
            raise ValueError("编码不能为空")
        with self._lock:
            existing = self._codes.get(normalized)
            if existing is None:
                self._codes[normalized] = (display or normalized, unit)
            elif display or unit:
                # 已存在：仅在提供了非空元数据时做确定性修正，单位缺失时保留原值
                self._codes[normalized] = (
                    display or existing[0],
                    unit or existing[1],
                )
            return normalized

    def register_alias(self, alias: str, target_code: str) -> str:
        """为已注册编码新增别名（大小写不敏感）。返回规范化后的标准编码。

        - 目标编码若以等价大小写存在会先规范化；
        - 别名已指向同一编码时幂等；指向其他编码时报错（不静默改映射）；
        - 目标编码未注册时抛 UnknownTargetCodeError，避免出现悬空别名。
        """
        alias_key = (alias or "").strip().upper()
        target = (target_code or "").strip().upper()
        if not alias_key:
            raise ValueError("别名不能为空")
        with self._lock:
            if target not in self._codes:
                raise UnknownTargetCodeError(
                    f"目标编码 {target} 尚未注册，请先 register_code"
                )
            current = self._aliases.get(alias_key)
            if current is not None and current != target:
                raise ValueError(
                    f"别名 {alias_key} 已指向 {current}，不能改指向 {target}"
                )
            self._aliases[alias_key] = target
            return target

    def known_codes(self) -> dict[str, tuple[str, str]]:
        """当前字典快照（用于管理接口展示）。"""
        with self._lock:
            return dict(self._codes)

    def aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._aliases)


# 默认全局注册表：供模块级函数与未显式注入注册表的调用方使用
DEFAULT_REGISTRY = CodeRegistry()


def normalize_code(raw: str) -> str | None:
    """归一原始编码（委托默认注册表）；无法识别时返回 None。"""
    return DEFAULT_REGISTRY.normalize_code(raw)
