"""直接标识符脱敏：持久化与所有读取路径只出现脱敏值。

- pseudonymize: HMAC-SHA256 假名化，不可逆，同一盐值下稳定（用于关联）。
- mask_free_text: 对错误定位用的原始片段做脱敏，替换疑似证件号/电话/姓名。
"""
from __future__ import annotations

import hashlib
import hmac
import re

_ID_NUMBER_RE = re.compile(r"\b\d{15,18}[0-9Xx]?\b")   # 疑似证件号
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")               # 疑似手机号
_LONG_DIGIT_RE = re.compile(r"\b\d{6,}\b")               # 其他长数字串


def pseudonymize(value: str, salt: str) -> str:
    """对直接标识符做不可逆假名化；空值返回占位符。"""
    if not value:
        return "<empty>"
    digest = hmac.new(salt.encode(), value.strip().encode(), hashlib.sha256)
    return "anon:" + digest.hexdigest()[:16]


def mask_free_text(fragment: str, salt: str) -> str:
    """对原始片段中可能的直接标识做脱敏，用于错误定位。"""
    masked = _ID_NUMBER_RE.sub(lambda m: pseudonymize(m.group(0), salt), fragment)
    masked = _PHONE_RE.sub(lambda m: pseudonymize(m.group(0), salt), masked)
    masked = _LONG_DIGIT_RE.sub(lambda m: pseudonymize(m.group(0), salt), masked)
    return masked
