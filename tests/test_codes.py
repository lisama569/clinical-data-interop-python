"""编码识别与单位换算测试。"""

from __future__ import annotations

import logging

import pytest

from app.codes import canonicalize, is_known
from app.errors import RecordRejected
from app.models import RejectReason

logger = logging.getLogger("clinical.interop.test")


def test_code_alias_tolerance() -> None:
    for raw in ("LOINC:2345-7", "loinc2345-7", "LOINC23457", "GLU", "local-glucose"):
        logger.info("输入摘要: code=%r 判定: 别名归一后应识别为 GLU", raw)
        assert is_known(raw)
        code, _, unit = canonicalize(raw, 1.0, "mmol/L")
        assert code == "GLU" and unit == "mmol/L"


def test_unit_conversion_glucose_mgdl_to_mmoll() -> None:
    logger.info("输入摘要: code=2345-7 value=180 unit=mg/dL 判定: 按 /18.0156 换算为 mmol/L")
    code, value, unit = canonicalize("2345-7", 180.156, "mg/dL")
    assert code == "GLU" and unit == "mmol/L"
    assert value == pytest.approx(10.0, rel=1e-6)


def test_unit_conversion_hgb_gdl_to_gl() -> None:
    logger.info("输入摘要: code=HGB value=13.2 unit=g/dL 判定: x10 换算为 g/L")
    code, value, unit = canonicalize("HGB", 13.2, "g/dL")
    assert (code, unit) == ("HGB", "g/L")
    assert value == pytest.approx(132.0)


def test_unit_conversion_wbc_per_ul() -> None:
    logger.info("输入摘要: code=WBC value=7000 unit=/uL 判定: x0.001 换算为 10^9/L")
    code, value, unit = canonicalize("WBC", 7000.0, "/uL")
    assert (code, unit) == ("WBC", "10^9/L")
    assert value == pytest.approx(7.0)


def test_unknown_code_rejected() -> None:
    logger.info("输入摘要: code='LOINC:9999-9' 判定: 不在目录 -> UNKNOWN_CODE")
    with pytest.raises(RecordRejected) as exc:
        canonicalize("LOINC:9999-9", 1.0, "mmol/L")
    assert exc.value.reason is RejectReason.UNKNOWN_CODE


def test_unit_mismatch_rejected() -> None:
    logger.info("输入摘要: code=GLU unit='parsec' 判定: 单位不兼容 -> UNIT_MISMATCH")
    with pytest.raises(RecordRejected) as exc:
        canonicalize("GLU", 1.0, "parsec")
    assert exc.value.reason is RejectReason.UNIT_MISMATCH


def test_reason_codes_are_distinguishable() -> None:
    logger.info("输入摘要: 未知编码 vs 错误单位 判定: 两者拒绝原因必须不同")
    with pytest.raises(RecordRejected) as a:
        canonicalize("nope", 1.0, "mmol/L")
    with pytest.raises(RecordRejected) as b:
        canonicalize("GLU", 1.0, "nope")
    assert a.value.reason != b.value.reason
