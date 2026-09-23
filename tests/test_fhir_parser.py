"""FHIR Bundle 解析器测试：每条用例打印输入摘要与判定依据。"""

from __future__ import annotations

import json
import logging

from app.models import RejectReason
from app.parsers.fhir import FhirParser

logger = logging.getLogger("clinical.interop.test")
parser = FhirParser()


def _patient(pid: str = "P1", **over) -> dict:
    base = {
        "resourceType": "Patient",
        "identifier": [{"system": "sysA", "value": pid,
                        "type": {"coding": [{"code": "IDCARD"}]}}],
        "name": [{"family": "Zhang", "given": ["San"]}],
        "telecom": [{"system": "phone", "value": "13800000000"}],
        "gender": "male",
        "birthDate": "1990-01-01",
    }
    base.update(over)
    return base


def _observation(oid: str = "O1", *, code="2345-7", value=5.5, unit="mmol/L", patient="P1", **over) -> dict:
    base = {
        "resourceType": "Observation",
        "identifier": [{"system": "sysA", "value": oid}],
        "status": "final",
        "subject": {"reference": f"Patient/{patient}"},
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": "2026-09-24T08:00:00",
    }
    base.update(over)
    return base


def _bundle(*resources: dict) -> str:
    return json.dumps(
        {"resourceType": "Bundle", "type": "transaction",
         "entry": [{"resource": r} for r in resources]}
    )


def test_malformed_json_rejected() -> None:
    logger.info("输入摘要: 非JSON负载 判定: MALFORMED, 不抛异常不丢批")
    report = parser.parse("{not-json")
    assert len(report.rejections) == 1
    assert report.rejections[0].reason is RejectReason.MALFORMED
    assert report.patients == [] and report.observations == []


def test_not_a_bundle_rejected() -> None:
    logger.info("输入摘要: 顶层 resourceType=Patient 判定: UNSUPPORTED_FORMAT")
    report = parser.parse(json.dumps({"resourceType": "Patient"}))
    assert report.rejections[0].reason is RejectReason.UNSUPPORTED_FORMAT


def test_valid_patient_tokens_no_plaintext() -> None:
    logger.info("输入摘要: 1个Patient 判定: 接受, 仅存令牌, 预览无明文")
    report = parser.parse(_bundle(_patient()))
    assert len(report.patients) == 1 and not report.rejections
    p = report.patients[0]
    assert p.name_token and len(p.name_token) == 64
    assert getattr(p, "name", None) is None


def test_missing_required_field() -> None:
    bad = _patient()
    bad["telecom"] = []
    logger.info("输入摘要: Patient 无 telecom 判定: MISSING_FIELD(phone)")
    report = parser.parse(_bundle(bad))
    assert report.rejections[0].reason is RejectReason.MISSING_FIELD
    assert "phone" in report.rejections[0].detail or "field" in report.rejections[0].detail


def test_invalid_value_distinguished() -> None:
    bad = _observation(value="abc")
    logger.info("输入摘要: valueQuantity.value='abc' 判定: INVALID_VALUE")
    report = parser.parse(_bundle(bad))
    assert report.rejections[0].reason is RejectReason.INVALID_VALUE


def test_unknown_code_distinguished() -> None:
    bad = _observation(code="9999-9")
    logger.info("输入摘要: code=9999-9 判定: UNKNOWN_CODE")
    report = parser.parse(_bundle(bad))
    assert report.rejections[0].reason is RejectReason.UNKNOWN_CODE


def test_unit_mismatch_distinguished() -> None:
    bad = _observation(unit="parsec")
    logger.info("输入摘要: unit=parsec 判定: UNIT_MISMATCH")
    report = parser.parse(_bundle(bad))
    assert report.rejections[0].reason is RejectReason.UNIT_MISMATCH


def test_observation_normalized() -> None:
    logger.info("输入摘要: GLU 180.156mg/dL 判定: 接受并换算为 10 mmol/L")
    report = parser.parse(_bundle(_observation(value=180.156, unit="mg/dL")))
    assert not report.rejections
    obs = report.observations[0]
    assert obs.canonical_code == "GLU"
    assert obs.canonical_unit == "mmol/L"
    assert obs.canonical_value == 10.0


def test_duplicate_in_batch_rejected() -> None:
    logger.info("输入摘要: 同 batch 两个 O1 判定: 第二个 DUPLICATE_IN_BATCH")
    report = parser.parse(_bundle(_observation("O1"), _observation("O1")))
    assert len(report.observations) == 1
    assert report.rejections[0].reason is RejectReason.DUPLICATE_IN_BATCH
    assert report.rejections[0].locator == "Bundle.entry[1]"


def test_mixed_batch_keeps_good_and_bad() -> None:
    logger.info("输入摘要: 好患者+坏观测+好观测 判定: 好记录保留, 坏记录按因拒绝, 定位可区分")
    payload = _bundle(_patient(), _observation(code="9999-9", oid="bad"), _observation("good"))
    report = parser.parse(payload)
    assert len(report.patients) == 1
    assert len(report.observations) == 1
    assert {r.reason for r in report.rejections} == {RejectReason.UNKNOWN_CODE}


def test_masked_preview_has_no_plaintext_direct_id() -> None:
    logger.info("输入摘要: 含手机号的坏记录 判定: masked_preview 不含原始号码")
    bad = _patient()
    bad["birthDate"] = "not-a-date"
    report = parser.parse(_bundle(bad))
    preview = report.rejections[0].masked_preview
    assert "13800000000" not in preview
    assert "Zhang" not in preview


def test_encounter_parsed_and_linked() -> None:
    logger.info("输入摘要: Patient+Encounter+Observation 判定: 就诊被解析, 观测关联就诊ID")
    encounter = {
        "resourceType": "Encounter",
        "identifier": [{"system": "sysA", "value": "E1"}],
        "status": "finished",
        "subject": {"reference": "Patient/P1"},
        "period": {"start": "2026-09-24T08:00:00", "end": "2026-09-24T09:00:00"},
    }
    obs_resource = _observation("O1")
    obs_resource["encounter"] = {"reference": "Encounter/E1"}
    report = parser.parse(_bundle(_patient(), encounter, obs_resource))
    assert not report.rejections
    assert len(report.encounters) == 1
    enc = report.encounters[0]
    assert enc.source_id == "E1" and enc.status == "finished"
    assert enc.patient_source_id == "P1"
    assert report.observations[0].encounter_source_id == "E1"


def test_encounter_bad_status_rejected() -> None:
    logger.info("输入摘要: Encounter.status=weird 判定: INVALID_VALUE")
    encounter = {
        "resourceType": "Encounter",
        "id": "E1",
        "status": "weird",
        "subject": {"reference": "Patient/P1"},
    }
    report = parser.parse(_bundle(encounter))
    assert report.rejections[0].reason is RejectReason.INVALID_VALUE
