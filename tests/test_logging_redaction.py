"""日志兜底脱敏测试。"""

from __future__ import annotations

import logging

from app.logging_setup import redact_text


def test_redact_direct_identifiers() -> None:
    raw = "ingest phone=13800000000 id_number: 110101199001011234 email a.b@example.com"
    out = redact_text(raw)
    assert "13800000000" not in out
    assert "110101199001011234" not in out
    assert "a.b@example.com" not in out


def test_log_record_filter_redacts_args(caplog) -> None:
    from app.logging_setup import RedactingFilter

    logger = logging.getLogger("clinical.interop.test.redact")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info("debug data phone=%s", "13800000000")
    assert "13800000000" not in caplog.text
