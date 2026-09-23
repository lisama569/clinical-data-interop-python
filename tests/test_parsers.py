"""畸形记录与非法字段：失败原因必须可区分，日志打印输入摘要与判定依据。"""
import json
import logging

import pytest

from app.errors import RecordRejectedError
from app.models import RejectReason
from app.parsers import parse_record

log = logging.getLogger("test.parsers")

FHIR_OK = json.dumps({
    "patient": {"id": "p1", "identifier": [{"value": "ID-001"}],
                "name": [{"text": "Zhang San"}], "telecom": [{"value": "13800000000"}]},
    "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
    "observations": [{"id": "o1", "code": {"coding": [{"code": "GLU"}]},
                      "valueQuantity": {"value": 5.6, "unit": "mg/dL"}}],
})

HL7_OK = (
    "MSH|^~\\&|LAB|HOSP\r"
    "PID|1||P123^^^HOSP||Zhang^San||M|||^^^13800000000\r"
    "OBR|1|ORD1|||panel||20260101100000\r"
    "OBX|1|NM|GLU^Glucose||5.6|mg/dL"
)


def _expect_reject(payload: str, reason: RejectReason, summary: str):
    log.info("输入摘要=%s 期望判定=%s", summary, reason.value)
    with pytest.raises(RecordRejectedError) as ei:
        parse_record(payload, "test-salt")
    assert ei.value.rejection.reason is reason, (
        f"判定依据不符: 期望 {reason}, 实际 {ei.value.rejection.reason}"
    )
    log.info("实际判定=%s 依据=%s", ei.value.rejection.reason.value, ei.value.rejection.detail)
    return ei.value.rejection


def test_fhir_ok():
    b = parse_record(FHIR_OK, "test-salt")
    assert b.patient and b.encounter and len(b.observations) == 1
    assert b.observations[0].code == "2345-7"


def test_hl7_ok():
    b = parse_record(HL7_OK, "test-salt")
    assert b.patient and b.encounter and len(b.observations) == 1


def test_unrecognized_format():
    r = _expect_reject("<<<not a record>>>", RejectReason.MALFORMED_FORMAT, "无法识别的格式")
    assert r.source_fragment  # 保留可定位片段


def test_broken_json():
    _expect_reject('{"patient": ', RejectReason.MALFORMED_FORMAT, "JSON 截断")


def test_missing_patient_identifier():
    payload = json.dumps({"patient": {"name": [{"text": "No Id"}]},
                          "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
                          "observations": []})
    _expect_reject(payload, RejectReason.MISSING_REQUIRED_FIELD, "缺 Patient.identifier")


def test_missing_encounter_period():
    payload = json.dumps({"patient": {"id": "p1"}, "encounter": {"id": "e1"}, "observations": []})
    _expect_reject(payload, RejectReason.MISSING_REQUIRED_FIELD, "缺 Encounter.period.start")


def test_unknown_observation_code():
    payload = json.dumps({
        "patient": {"id": "p1"},
        "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
        "observations": [{"id": "o1", "code": {"coding": [{"code": "NOPE-1"}]},
                          "valueQuantity": {"value": 1}}]})
    r = _expect_reject(payload, RejectReason.UNKNOWN_CODE, "未知检验编码")
    assert "NOPE-1" in r.detail


def test_hl7_missing_obr():
    msg = "MSH|^~\\&|L|H\rPID|1||P1^^^H\rOBX|1|NM|GLU^G||5.0|mg/dL"
    _expect_reject(msg, RejectReason.MISSING_REQUIRED_FIELD, "HL7 缺 OBR 段")


def test_hl7_unknown_code():
    msg = ("MSH|^~\\&|L|H\rPID|1||P1^^^H\rOBR|1|O1|||p||20260101\r"
           "OBX|1|NM|XYZ9^Unknown||5.0|mg/dL")
    _expect_reject(msg, RejectReason.UNKNOWN_CODE, "HL7 未知编码")


def test_rejection_fragment_is_masked():
    """拒绝原因中的原始片段不得泄露证件号/电话原文。"""
    payload = json.dumps({"patient": {"id": "110101199003077777",
                                      "telecom": [{"value": "13812345678"}]},
                          "observations": "not-a-list-trigger"})
    # 构造一个会拒绝且片段含敏感值的输入
    r = _expect_reject('{"patient": {"telecom": [{"value": "13812345678"}]}, "x": 1}',
                       RejectReason.MISSING_REQUIRED_FIELD, "片段含手机号")
    assert "13812345678" not in r.source_fragment
