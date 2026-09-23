"""服务编排测试：幂等、并发、越权、脱敏与更新冲突结论。"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import AccessDenied, BatchInconsistent, ConflictError
from app.models import ConflictVerdict, Format, IngestStatus, RejectReason, Role
from app.repository import Repository
from app.service import IngestionService

logger = logging.getLogger("clinical.interop.test")


def _patient_json(pid: str = "P1", name="Zhang San", phone="13800000000", idv="110101199001011234") -> dict:
    return {
        "resourceType": "Patient",
        "identifier": [
            {
                "system": "sysA",
                "value": pid,
                "type": {"coding": [{"code": "IDCARD"}]},
            }
        ],
        "name": [{"text": name}],
        "telecom": [{"system": "phone", "value": phone}],
        "gender": "male",
        "birthDate": "1990-01-01",
    }


def _obs_json(oid: str = "O1", *, code="2345-7", value=5.5, unit="mmol/L", patient="P1") -> dict:
    return {
        "resourceType": "Observation",
        "identifier": [{"system": "sysA", "value": oid}],
        "status": "final",
        "subject": {"reference": f"Patient/{patient}"},
        "code": {"coding": [{"code": code}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": "2026-09-24T08:00:00",
    }


def _bundle(*resources: dict) -> str:
    return json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]})


@pytest.fixture()
def svc() -> IngestionService:
    return IngestionService(Repository())


GOOD_BUNDLE = _bundle(_patient_json(), _obs_json())


def test_ingest_happy_path(svc: IngestionService) -> None:
    logger.info("输入摘要: FHIR 1患者+1观测 role=ingest 判定: ACCEPTED 计数正确")
    result = svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    assert result.status is IngestStatus.ACCEPTED
    assert result.accepted_counts == {"Patient": 1, "Encounter": 0, "Observation": 1}
    assert result.replay is False


def test_ingest_requires_permission(svc: IngestionService) -> None:
    logger.info("输入摘要: role=anonymous 提交 判定: AccessDenied, 消息无原始数据")
    with pytest.raises(AccessDenied):
        svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.ANONYMOUS)


def test_batch_replay_is_idempotent(svc: IngestionService) -> None:
    logger.info("输入摘要: 相同 batch_id+相同负载 提交两次 判定: 第二次 REPLAYED 且计数为0")
    first = svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    second = svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    assert first.replay is False and second.replay is True
    assert second.status is IngestStatus.REPLAYED
    assert sum(second.accepted_counts.values()) == 0
    assert len(svc.export_observations(role=Role.EXPORTER)) == 1  # 无重复记录


def test_same_batch_id_different_payload_rejected(svc: IngestionService) -> None:
    other = _bundle(_obs_json("O9"))
    logger.info("输入摘要: 同 batch_id 不同负载 判定: BatchInconsistent, 首批次结论保留")
    svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    with pytest.raises(BatchInconsistent):
        svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=other, role=Role.INGEST)
    assert len(svc.export_observations(role=Role.EXPORTER)) == 1


def test_concurrent_duplicate_batch_submission(svc: IngestionService) -> None:
    logger.info("输入摘要: 10 线程并发提交同一批次 判定: 仅1次写入, 其余 REPLAYED")
    outcomes: list[str] = []

    def submit(_: int) -> None:
        result = svc.ingest_batch(batch_id="BC", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
        outcomes.append("replay" if result.replay else "first")

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(submit, range(10)))
    assert outcomes.count("first") == 1
    assert outcomes.count("replay") == 9
    assert len(svc.export_observations(role=Role.EXPORTER)) == 1


def test_rejections_are_retained_with_reasons(svc: IngestionService) -> None:
    payload = _bundle(_obs_json("bad", code="9999-9"), _obs_json("good"))
    logger.info("输入摘要: 1未知编码+1好观测 判定: PARTIAL, 拒绝可按索引与原因检索")
    result = svc.ingest_batch(batch_id="BR", fmt=Format.FHIR_JSON, payload=payload, role=Role.INGEST)
    assert result.status is IngestStatus.PARTIAL
    assert result.rejections[0].reason is RejectReason.UNKNOWN_CODE
    listed = svc.list_rejections(batch_id="BR", role=Role.AUDITOR)
    assert listed[0]["locator"] == "Bundle.entry[0]"
    assert listed[0]["reason"] == "UNKNOWN_CODE"


def test_reader_cannot_list_rejections_or_export(svc: IngestionService) -> None:
    logger.info("输入摘要: role=reader 访问拒绝清单与导出 判定: 均 AccessDenied")
    with pytest.raises(AccessDenied):
        svc.list_rejections(batch_id=None, role=Role.READER)
    with pytest.raises(AccessDenied):
        svc.export_observations(role=Role.READER)


def test_patient_view_is_masked(svc: IngestionService) -> None:
    svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    logger.info("输入摘要: reader 查询患者 判定: 仅令牌, 无姓名/证件/手机键与值")
    view = svc.get_patient_view(source_system="sysA", source_id="P1", role=Role.READER)
    assert "Zhang" not in json.dumps(view, ensure_ascii=False)
    assert "13800000000" not in json.dumps(view)
    assert "110101199001011234" not in json.dumps(view)
    assert set(view) >= {"name_token", "ident_token", "contact_token"}
    assert "name" not in view and "phone" not in view and "id_number" not in view


def test_anonymous_cannot_read_patient(svc: IngestionService) -> None:
    svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    logger.info("输入摘要: anonymous 查患者 判定: AccessDenied")
    with pytest.raises(AccessDenied):
        svc.get_patient_view(source_system="sysA", source_id="P1", role=Role.ANONYMOUS)


def test_auditor_fragment_is_masked(svc: IngestionService) -> None:
    # 制造一个坏患者（非法日期）以便进入隔离区
    bad = _patient_json()
    bad["birthDate"] = "nope"
    svc.ingest_batch(batch_id="BF", fmt=Format.FHIR_JSON, payload=_bundle(bad), role=Role.INGEST)
    logger.info("输入摘要: auditor 取坏记录片段 判定: 片段中直接标识已遮蔽")
    frag = svc.get_rejected_fragment(batch_id="BF", index=0, role=Role.AUDITOR)
    assert "13800000000" not in frag["fragment_masked"]
    assert "Zhang San" not in frag["fragment_masked"]


def test_non_auditor_cannot_get_fragment(svc: IngestionService) -> None:
    bad = _patient_json(); bad["birthDate"] = "nope"
    svc.ingest_batch(batch_id="BF", fmt=Format.FHIR_JSON, payload=_bundle(bad), role=Role.INGEST)
    logger.info("输入摘要: exporter 取原始片段 判定: AccessDenied")
    with pytest.raises(AccessDenied):
        svc.get_rejected_fragment(batch_id="BF", index=0, role=Role.EXPORTER)


def test_optimistic_update_conflict_verdicts(svc: IngestionService) -> None:
    svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    logger.info("输入摘要: A/B 都基于 v1 更新 判定: A成功v2, B得 VERSION_CONFLICT 且未覆盖")
    dto_a, va = svc.update_observation(
        source_system="sysA", source_id="O1", value=6.0, unit="mmol/L",
        expected_version=1, role=Role.ADMIN,
    )
    assert va is ConflictVerdict.NO_CONFLICT and dto_a["version"] == 2
    with pytest.raises(ConflictError) as exc:
        svc.update_observation(
            source_system="sysA", source_id="O1", value=9.0, unit="mmol/L",
            expected_version=1, role=Role.ADMIN,
        )
    assert exc.value.verdict is ConflictVerdict.VERSION_CONFLICT
    # 当前值保持 A 的 6.0，B 未覆盖
    assert svc.export_observations(role=Role.EXPORTER)[0]["value"] == 6.0


def test_idempotent_update_replay(svc: IngestionService) -> None:
    svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    logger.info("输入摘要: 用相同值+当前版本更新 判定: IDEMPOTENT_REPLAY, 版本不变")
    dto, verdict = svc.update_observation(
        source_system="sysA", source_id="O1", value=5.5, unit="mmol/L",
        expected_version=1, role=Role.ADMIN,
    )
    assert verdict is ConflictVerdict.IDEMPOTENT_REPLAY
    assert dto["version"] == 1


def test_reader_cannot_update(svc: IngestionService) -> None:
    svc.ingest_batch(batch_id="B1", fmt=Format.FHIR_JSON, payload=GOOD_BUNDLE, role=Role.INGEST)
    logger.info("输入摘要: reader 更新观测 判定: AccessDenied")
    with pytest.raises(AccessDenied):
        svc.update_observation(
            source_system="sysA", source_id="O1", value=6.0, unit="mmol/L",
            expected_version=1, role=Role.READER,
        )


def test_csv_ingest_and_dedup(svc: IngestionService) -> None:
    from app.parsers.csv_parser import REQUIRED_COLUMNS

    header = ",".join(REQUIRED_COLUMNS)
    row = "sysA,P2,李四,310101199002022345,13900001111,O2,HGB,13.2,g/dL,2026-09-24T08:00:00"
    logger.info("输入摘要: CSV 1行 HGB g/dL 判定: 接受并换算 132 g/L, 无明文外泄")
    result = svc.ingest_batch(batch_id="BC1", fmt=Format.CSV, payload=f"{header}\n{row}\n", role=Role.INGEST)
    assert result.status is IngestStatus.ACCEPTED
    exported = svc.export_observations(role=Role.EXPORTER)[0]
    assert exported["code"] == "HGB" and exported["value"] == 132.0 and exported["unit"] == "g/L"
    dumped = json.dumps(svc.list_rejections(batch_id="BC1", role=Role.AUDITOR), ensure_ascii=False)
    assert "李四" not in dumped and "13900001111" not in dumped
