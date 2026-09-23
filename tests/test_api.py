"""HTTP 端到端集成测试。"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from main import app

logger = logging.getLogger("clinical.interop.test")


@pytest.fixture(autouse=True)
def _fresh_service():
    api_mod.service = api_mod.IngestionService()
    yield


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


PATIENT = {
    "resourceType": "Patient",
    "identifier": [{"system": "sysA", "value": "P1",
                    "type": {"coding": [{"code": "IDCARD"}]}}],
    "name": [{"text": "Zhang San"}],
    "telecom": [{"system": "phone", "value": "13800000000"}],
    "gender": "male",
    "birthDate": "1990-01-01",
}


def obs(oid="O1", code="2345-7", value=5.5, unit="mmol/L") -> dict:
    return {
        "resourceType": "Observation",
        "identifier": [{"system": "sysA", "value": oid}],
        "status": "final",
        "subject": {"reference": "Patient/P1"},
        "code": {"coding": [{"code": code}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": "2026-09-24T08:00:00",
    }


def bundle(*resources) -> bytes:
    return json.dumps(
        {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}
    ).encode()


def test_root(client: TestClient) -> None:
    assert client.get("/").json()["status"] == "ok"


def test_ingest_and_replay(client: TestClient) -> None:
    payload = bundle(PATIENT, obs())
    headers = {"X-Role": "ingest", "Content-Type": "application/json"}
    logger.info("输入摘要: POST /ingest/B1 两次相同负载 判定: 首次 accepted, 第二次 replayed")
    r1 = client.post("/ingest/B1?f=fhir-json", content=payload, headers=headers)
    r2 = client.post("/ingest/B1?f=fhir-json", content=payload, headers=headers)
    assert r1.status_code == 200 and r1.json()["status"] == "ACCEPTED"
    assert r2.json()["status"] == "REPLAYED" and r2.json()["replay"] is True


def test_concurrent_duplicate_batch_http(client: TestClient) -> None:
    payload = bundle(PATIENT, obs())
    headers = {"X-Role": "ingest"}
    logger.info("输入摘要: 12 并发 POST 同批次 判定: 恰1次 ACCEPTED, 其余 REPLAYED")
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(
            lambda _: client.post("/ingest/BC?f=fhir-json", content=payload, headers=headers),
            range(12),
        ))
    statuses = [r.json()["status"] for r in responses]
    assert statuses.count("ACCEPTED") == 1
    assert statuses.count("REPLAYED") == 11


def test_malformed_and_unknown_code_distinguishable(client: TestClient) -> None:
    headers = {"X-Role": "ingest"}
    logger.info("输入摘要: 畸形JSON + 未知编码观测 判定: 响应中原因码分别为 MALFORMED/UNKNOWN_CODE")
    r_bad = client.post("/ingest/BM?f=fhir-json", content=b"{bad json", headers=headers)
    assert r_bad.json()["rejections"][0]["reason"] == "MALFORMED"
    r_code = client.post(
        "/ingest/BC2?f=fhir-json", content=bundle(obs(oid="x", code="9999-9")), headers=headers
    )
    assert r_code.json()["rejections"][0]["reason"] == "UNKNOWN_CODE"


def test_unauthorized_ingest_and_read(client: TestClient) -> None:
    logger.info("输入摘要: 无 X-Role 提交与查询 判定: 均 403")
    payload = bundle(PATIENT, obs())
    assert client.post("/ingest/B1?f=fhir-json", content=payload).status_code == 403
    assert client.get("/patients/sysA/P1").status_code == 403


def test_reader_patient_view_masked_and_export_denied(client: TestClient) -> None:
    client.post("/ingest/B1?f=fhir-json", content=bundle(PATIENT, obs()), headers={"X-Role": "ingest"})
    logger.info("输入摘要: reader 查患者并尝试导出 判定: 视图无明文, 导出 403")
    r = client.get("/patients/sysA/P1", headers={"X-Role": "reader"})
    body = r.text
    assert r.status_code == 200
    assert "13800000000" not in body and "Zhang San" not in body
    assert client.get("/export/observations", headers={"X-Role": "reader"}).status_code == 403
    # exporter 可导出，但数据中同样无直接标识
    exported = client.get("/export/observations", headers={"X-Role": "exporter"}).json()
    assert exported[0]["code"] == "GLU"
    assert "13800000000" not in json.dumps(exported)


def test_rejections_rbac_and_locator(client: TestClient) -> None:
    client.post("/ingest/BR?f=fhir-json", content=bundle(obs(oid="x", code="9999-9")),
                headers={"X-Role": "ingest"})
    logger.info("输入摘要: reader 拒绝访问 rejections, auditor 可见 locator/reason")
    assert client.get("/rejections", headers={"X-Role": "reader"}).status_code == 403
    listed = client.get("/rejections?batch_id=BR", headers={"X-Role": "auditor"}).json()
    assert listed[0]["locator"] == "Bundle.entry[0]"
    assert listed[0]["reason"] == "UNKNOWN_CODE"


def test_fragment_endpoint_masked(client: TestClient) -> None:
    bad = dict(PATIENT); bad["birthDate"] = "nope"
    client.post("/ingest/BF?f=fhir-json", content=bundle(bad), headers={"X-Role": "ingest"})
    logger.info("输入摘要: auditor 取片段 判定: 200 但直接标识已遮蔽; exporter 403")
    r = client.get("/rejections/BF/0/fragment", headers={"X-Role": "auditor"})
    assert r.status_code == 200
    assert "13800000000" not in r.text and "Zhang San" not in r.text
    assert client.get("/rejections/BF/0/fragment", headers={"X-Role": "exporter"}).status_code == 403


def test_update_conflict_http_semantics(client: TestClient) -> None:
    client.post("/ingest/B1?f=fhir-json", content=bundle(PATIENT, obs()), headers={"X-Role": "ingest"})
    url = "/observations/sysA/O1"
    headers = {"X-Role": "admin"}
    logger.info("输入摘要: A/B 同基于 v1 更新 判定: A 200 v2, B 409 VERSION_CONFLICT")
    r_a = client.put(url, json={"value": 6.0, "unit": "mmol/L", "expected_version": 1}, headers=headers)
    assert r_a.status_code == 200 and r_a.json()["observation"]["version"] == 2
    r_b = client.put(url, json={"value": 9.0, "unit": "mmol/L", "expected_version": 1}, headers=headers)
    assert r_b.status_code == 409 and r_b.json()["detail"]["verdict"] == "VERSION_CONFLICT"
    # 当前值保持 6.0
    exported = client.get("/export/observations", headers={"X-Role": "exporter"}).json()
    assert exported[0]["value"] == 6.0


def test_update_idempotent_replay_http(client: TestClient) -> None:
    client.post("/ingest/B1?f=fhir-json", content=bundle(PATIENT, obs()), headers={"X-Role": "ingest"})
    r = client.put(
        "/observations/sysA/O1",
        json={"value": 5.5, "unit": "mmol/L", "expected_version": 1},
        headers={"X-Role": "admin"},
    )
    logger.info("输入摘要: 相同值更新 判定: 200 IDEMPOTENT_REPLAY")
    assert r.status_code == 200 and r.json()["verdict"] == "IDEMPOTENT_REPLAY"


def test_update_requires_admin(client: TestClient) -> None:
    client.post("/ingest/B1?f=fhir-json", content=bundle(PATIENT, obs()), headers={"X-Role": "ingest"})
    r = client.put(
        "/observations/sysA/O1",
        json={"value": 6.0, "unit": "mmol/L", "expected_version": 1},
        headers={"X-Role": "reader"},
    )
    assert r.status_code == 403
    assert "13800000000" not in r.text


def test_csv_ingest_http(client: TestClient) -> None:
    from app.parsers.csv_parser import REQUIRED_COLUMNS

    header = ",".join(REQUIRED_COLUMNS)
    row = "sysA,P9,王五,32010119850303456X,13700007777,O9,CREA,1.0,mg/dL,2026-09-24T08:00:00"
    body = f"{header}\n{row}\n"
    logger.info("输入摘要: CSV CREA mg/dL 判定: 接受并换算 umol/L")
    r = client.post("/ingest/BV?f=csv", content=body.encode(), headers={"X-Role": "ingest"})
    assert r.status_code == 200 and r.json()["accepted_counts"]["Observation"] == 1
    exported = client.get("/export/observations", headers={"X-Role": "exporter"}).json()
    assert exported[0]["code"] == "CREA" and exported[0]["value"] == 88.4
    assert "王五" not in json.dumps(exported) and "13700007777" not in json.dumps(exported)
