"""直接标识符脱敏：持久化与所有读取路径只出现脱敏值。

- pseudonymize: HMAC-SHA256 假名化，不可逆，同一盐值下稳定（用于关联）。
- mask_free_text: 对错误定位用的原始片段做脱敏，替换疑似证件号/电话/长数字串。
- mask_payload_fragment: 报文感知脱敏，在数字规则之外额外遮蔽 FHIR/HL7 中的姓名、
  患者标识与联系方式字段，保证待处理区列表/明细/错误响应/日志中不出现姓名原文。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re

_ID_NUMBER_RE = re.compile(r"\b\d{15,18}[0-9Xx]?\b")   # 疑似证件号
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")               # 疑似手机号
_LONG_DIGIT_RE = re.compile(r"\b\d{6,}\b")               # 其他长数字串

# FHIR JSON 中承载姓名的标量字段：Patient.name[].text / family
_JSON_NAME_SCALAR_RE = re.compile(r'("(?:text|family)"\s*:\s*)"([^"]*)"')
# Patient.name[].given 是字符串数组，逐个遮蔽其中字符串
_JSON_GIVEN_RE = re.compile(r'("given"\s*:\s*\]?)(.*?)(\])', re.DOTALL)
_JSON_QUOTED_RE = re.compile(r'"([^"]*)"')


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


def _mask_json_names(text: str, salt: str) -> str:
    def _scalar(m: re.Match) -> str:
        return m.group(1) + json.dumps(pseudonymize(m.group(2), salt))

    def _given(m: re.Match) -> str:
        inner = _JSON_QUOTED_RE.sub(
            lambda q: json.dumps(pseudonymize(q.group(1), salt)), m.group(2)
        )
        return m.group(1) + inner + m.group(3)

    text = _JSON_NAME_SCALAR_RE.sub(_scalar, text)
    text = _JSON_GIVEN_RE.sub(_given, text)
    return text


def _mask_hl7_pid(text: str, salt: str) -> str:
    """遮蔽 PID 段中的直接标识：PID-3 患者标识、PID-5 姓名、PID-13 联系方式。"""
    out_lines = []
    for line in text.replace("\r", "\n").split("\n"):
        if line.startswith("PID"):
            seg = line.split("|")
            for idx in (3, 5, 13):  # 1-based 字段号（seg[0] 为段名 PID）
                if idx < len(seg) and seg[idx].strip():
                    seg[idx] = pseudonymize(seg[idx], salt)
            line = "|".join(seg)
        out_lines.append(line)
    return "\n".join(out_lines)


def mask_payload_fragment(payload: str, salt: str) -> str:
    """对整条原始报文先做结构脱敏再截取前 300 字符，作为可定位但无直接标识的片段。"""
    text = payload
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        text = _mask_json_names(text, salt)
    if "PID|" in text:
        text = _mask_hl7_pid(text, salt)
    # 数字规则兜底：证件号/手机号/其他长数字串
    return mask_free_text(text, salt)[:300]
