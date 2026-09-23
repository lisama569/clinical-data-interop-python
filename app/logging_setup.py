"""日志配置：所有日志输出前统一脱敏。

防线分两层：
1. 业务代码只记录 locator / reason / 字段名，不记录直接标识值；
2. ``RedactingFilter`` 在格式化前对消息做兜底遮蔽，识别常见的
   身份证号（18/15 位）、手机号、邮箱与 ``key=value`` / ``key: value`` 中的标识键。
"""

from __future__ import annotations

import logging
import re

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
_CN_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)|(?<!\d)\d{15}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_KEYED_RE = re.compile(
    r"((?:patient_)?(?:name|id_number|id_no|phone|mobile|email|contact_name)"
    r"|family|given|passport|telecom)(\s*[=:]\s*)([^,\s}\]\)]+)",
    re.IGNORECASE,
)


def redact_text(text: str) -> str:
    text = _CN_ID_RE.sub("<redacted:id>", text)
    text = _PHONE_RE.sub("<redacted:phone>", text)
    text = _EMAIL_RE.sub("<redacted:email>", text)
    text = _KEYED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    return text


class RedactingFilter(logging.Filter):
    """确保日志记录中不出现直接标识原始值。

    仅净化参数与"已成形消息"（无占位符的 msg），避免破坏 ``%s`` 风格格式化。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _safe(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_safe(a) for a in record.args)
        elif isinstance(record.msg, str) and "%" not in record.msg:
            record.msg = redact_text(record.msg)
        return True


def _safe(value: object) -> object:
    return redact_text(value) if isinstance(value, str) else value


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    # 挂在 logger 上可保证所有 handler（含测试捕获 handler）都经过脱敏
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
