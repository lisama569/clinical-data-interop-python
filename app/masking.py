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


def fingerprint_payload(payload: str, salt: str) -> str:
    """报文内容指纹（HMAC-SHA256）：用于同内容重复进入待处理区的幂等去重。

    指纹不可逆推出原文，可安全出现在日志中。
    """
    digest = hmac.new(
        b"quarantine-fingerprint:" + salt.encode(),
        payload.encode(),
        hashlib.sha256,
    )
    return "fp:" + digest.hexdigest()[:32]


# FHIR 姓名字段（本子集内这些键只出现在 Patient.name）：遮蔽其字符串值
_FHIR_NAME_SCALAR_RE = re.compile(r'("(?:text|family)"\s*:\s*)"[^"]*"')
_FHIR_NAME_GIVEN_RE = re.compile(r'("given"\s*:\s*)\[[^\]]*\]')
_NAME_PLACEHOLDER = '"<masked-name>"'
_FIELD_PLACEHOLDER = "<masked-name>"


def _mask_hl7_pid_names(text: str) -> str:
    """遮蔽 HL7 报文中 PID 段的姓名字段（PID-5）。"""
    out_lines: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        if line.startswith("PID"):
            fields = line.split("|")
            if len(fields) > 5:
                fields[5] = _FIELD_PLACEHOLDER
            line = "|".join(fields)
        out_lines.append(line)
    return "\n".join(out_lines)


def mask_sensitive_context(payload: str, salt: str) -> str:
    """待处理区专用的报文上下文脱敏。

    在数字标识（证件号/电话/长数字串）脱敏之上，额外结构化遮蔽姓名：
    - FHIR JSON 中 Patient.name 的 text/family/given 值；
    - HL7 v2 中 PID-5 姓名。
    仅用于持久化的查阅上下文，不参与重放判定（重放走独立保存的原始报文）。
    """
    masked = mask_free_text(payload, salt)
    masked = _FHIR_NAME_SCALAR_RE.sub(lambda m: m.group(1) + _NAME_PLACEHOLDER, masked)
    masked = _FHIR_NAME_GIVEN_RE.sub(
        lambda m: m.group(1) + "[" + _NAME_PLACEHOLDER + "]", masked
    )
    masked = _mask_hl7_pid_names(masked)
    return masked
