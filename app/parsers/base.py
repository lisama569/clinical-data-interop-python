"""格式探测与解析分发。"""
from __future__ import annotations

from enum import Enum

from app.codes import DEFAULT_REGISTRY, CodeRegistry
from app.errors import RecordRejectedError
from app.masking import mask_free_text
from app.models import NormalizedBundle, Rejection, RejectReason
from app.parsers.fhir_json import parse_fhir_json
from app.parsers.hl7v2 import parse_hl7v2


class ExchangeFormat(str, Enum):
    FHIR_JSON = "fhir_json"
    HL7V2 = "hl7v2"


def detect_format(payload: str) -> ExchangeFormat | None:
    """根据报文内容探测交换格式；无法识别返回 None。"""
    text = payload.lstrip()
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        return ExchangeFormat.FHIR_JSON
    if text.startswith("MSH|") or text.startswith("MSH^"):
        return ExchangeFormat.HL7V2
    return None


def parse_record(
    payload: str,
    salt: str,
    registry: CodeRegistry | None = None,
) -> NormalizedBundle:
    """解析并归一一条源记录；失败抛出 RecordRejectedError。

    registry 缺省时使用进程级默认字典；注入自定义注册表可读到运行时最新编码。
    """
    reg = registry or DEFAULT_REGISTRY
    fmt = detect_format(payload)
    if fmt is None:
        raise RecordRejectedError(
            Rejection(
                reason=RejectReason.MALFORMED_FORMAT,
                detail="无法识别的交换格式（既非 FHIR JSON 也非 HL7 v2）",
                source_fragment=mask_free_text(payload[:300], salt),
            )
        )
    if fmt is ExchangeFormat.FHIR_JSON:
        return parse_fhir_json(payload, salt, reg)
    return parse_hl7v2(payload, salt, reg)
