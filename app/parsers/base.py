"""解析器基类与公共工具。

约定：单条记录出问题必须产出一条 :class:`Rejection`（带原因与可定位 locator），
既不静默丢弃，也不让整批失败；只有整批负载在格式级无法解析时才由调用方处理。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.masking import mask_record
from app.models import ParseReport, RawEntry, Rejection, RejectReason

logger = logging.getLogger("clinical.interop.parser")

_ISO_DT_RE = __import__("re").compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_ISO_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
_ENCOUNTER_STATUSES = {"planned", "in-progress", "finished", "cancelled"}
_OBS_STATUSES = {"registered", "preliminary", "final", "amended"}
_GENDERS = {"male", "female", "other", "unknown"}


def make_preview(raw: str, *, limit: int = 160) -> str:
    """脱敏后截断的片段预览（永不包含直接标识明文）。

    必须先对完整片段脱敏再截断——否则截断会破坏 JSON 结构，
    导致结构化脱敏无法生效而泄漏标识。非 JSON 片段返回固定遮蔽串
    （CSV 行由 CSV 解析器按列名提供结构化预览）。
    """

    masked: str
    try:
        structured = json.loads(raw)
        masked = json.dumps(mask_record(structured), ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        masked = "<redacted: unstructured fragment>"
    if len(masked) > limit:
        return masked[:limit] + "...<truncated>"
    return masked


def reject(
    report: ParseReport,
    entry: RawEntry | None,
    reason: RejectReason,
    detail: str,
    *,
    resource_type=None,
    preview: str | None = None,
) -> Rejection:
    """登记一条拒绝记录并写判定日志（不含原始值）。

    preview 可由解析器显式提供（如 CSV 按列名脱敏后的 dict）；
    缺省时对原始片段做通用脱敏。
    """

    index = entry.index if entry else -1
    locator = entry.locator if entry else "payload"
    masked_preview = preview if preview is not None else (make_preview(entry.raw) if entry else "")
    rejection = Rejection(
        index=index,
        locator=locator,
        reason=reason,
        detail=detail,
        masked_preview=masked_preview,
        resource_type=resource_type,
    )
    report.rejections.append(rejection)
    logger.info(
        "记录拒绝 locator=%s reason=%s basis=%s",
        locator,
        reason.value,
        detail,
    )
    return rejection


def require_fields(obj: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    """返回第一个缺失（不存在或为空字符串）的字段名，全部存在返回 None。"""

    for name in fields:
        value = obj.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return name
    return None


def valid_datetime(value: str) -> bool:
    return bool(_ISO_DT_RE.match(value.strip()))


def valid_date(value: str) -> bool:
    return bool(_ISO_DATE_RE.match(value.strip()))


def valid_encounter_status(value: str) -> bool:
    return value in _ENCOUNTER_STATUSES


def valid_obs_status(value: str) -> bool:
    return value in _OBS_STATUSES


def valid_gender(value: str) -> bool:
    return value in _GENDERS


class BaseParser:
    format_name: str = "base"

    def parse(self, payload: str | bytes) -> ParseReport:
        raise NotImplementedError
