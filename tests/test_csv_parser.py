"""CSV 解析器测试。"""

from __future__ import annotations

import logging

from app.models import RejectReason
from app.parsers.csv_parser import REQUIRED_COLUMNS, CsvParser

logger = logging.getLogger("clinical.interop.test")
parser = CsvParser()

HEADER = ",".join(REQUIRED_COLUMNS + ("gender", "status"))
ROW = "sysA,P1,张三,110101199001011234,13800000000,O1,2345-7,5.5,mmol/L,2026-09-24T08:00:00,male,final"


def test_valid_row_normalized() -> None:
    logger.info("输入摘要: 1行合法CSV 判定: 1患者+1观测, 患者仅令牌化")
    report = parser.parse(f"{HEADER}\n{ROW}\n")
    assert not report.rejections
    assert len(report.patients) == 1 and len(report.observations) == 1
    assert report.observations[0].canonical_code == "GLU"


def test_unit_conversion_in_csv() -> None:
    logger.info("输入摘要: GLU 180.156 mg/dL 判定: 换算 10 mmol/L")
    row = ROW.replace("5.5,mmol/L", "180.156,mg/dL")
    report = parser.parse(f"{HEADER}\n{row}\n")
    assert not report.rejections
    assert report.observations[0].canonical_value == 10.0


def test_missing_column_header_malformed() -> None:
    logger.info("输入摘要: 表头缺 unit 列 判定: 整批 MALFORMED 并指出缺列名")
    bad_header = HEADER.replace(",unit,", ",")
    report = parser.parse(f"{bad_header}\n{ROW}\n")
    assert report.rejections[0].reason is RejectReason.MALFORMED
    assert "unit" in report.rejections[0].detail


def test_wrong_column_count_row_malformed() -> None:
    logger.info("输入摘要: 数据行列数与表头不符 判定: 行级 MALFORMED, 其余行不受影响")
    payload = f"{HEADER}\nonly,three,cols\n{ROW}\n"
    report = parser.parse(payload)
    assert report.rejections[0].reason is RejectReason.MALFORMED
    assert report.rejections[0].locator == "csv:row=2"
    assert len(report.observations) == 1


def test_missing_field_row() -> None:
    row = ROW.replace("13800000000", "")
    logger.info("输入摘要: phone 为空 判定: MISSING_FIELD(phone)")
    report = parser.parse(f"{HEADER}\n{row}\n")
    assert report.rejections[0].reason is RejectReason.MISSING_FIELD
    assert "phone" in report.rejections[0].detail


def test_invalid_value_vs_unknown_code_vs_unit() -> None:
    logger.info("输入摘要: 三行分别坏值/坏码/坏单位 判定: 三种可区分原因")
    r_badval = ROW.replace("O1,2345-7,5.5", "Ov,2345-7,xyz")
    r_badcode = ROW.replace("O1,2345-7,5.5", "Oc,9999-9,5.5").replace("13800000000", "13800000001")
    r_badunit = ROW.replace("O1,2345-7,5.5,mmol/L", "Ou,2345-7,5.5,parsec").replace("13800000000", "13800000002")
    report = parser.parse(f"{HEADER}\n{r_badval}\n{r_badcode}\n{r_badunit}\n")
    reasons = {r.reason for r in report.rejections}
    assert reasons == {RejectReason.INVALID_VALUE, RejectReason.UNKNOWN_CODE, RejectReason.UNIT_MISMATCH}


def test_duplicate_observation_in_batch() -> None:
    logger.info("输入摘要: 同 O1 两行 判定: 第一行接受, 第二行 DUPLICATE_IN_BATCH")
    report = parser.parse(f"{HEADER}\n{ROW}\n{ROW}\n")
    assert len(report.observations) == 1
    assert report.rejections[0].reason is RejectReason.DUPLICATE_IN_BATCH


def test_same_patient_referenced_twice_is_idempotent_not_rejected() -> None:
    row2 = ROW.replace("O1,", "O2,").replace("2026-09-24T08:00:00", "2026-09-24T09:00:00")
    logger.info("输入摘要: 同患者两条不同观测 判定: 患者去重为1, 两条观测都接受")
    report = parser.parse(f"{HEADER}\n{ROW}\n{row2}\n")
    assert len(report.patients) == 1
    assert len(report.observations) == 2
    assert not report.rejections


def test_masked_preview_no_plaintext() -> None:
    row = ROW.replace("5.5", "notnum")
    logger.info("输入摘要: 坏值行含姓名/证件/手机 判定: 预览与判定依据中无原始值")
    report = parser.parse(f"{HEADER}\n{row}\n")
    rej = report.rejections[0]
    assert "张三" not in rej.masked_preview
    assert "110101199001011234" not in rej.masked_preview
    assert "13800000000" not in rej.masked_preview
    assert "张三" not in rej.detail and "13800000000" not in rej.detail
