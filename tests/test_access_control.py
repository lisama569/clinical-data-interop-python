"""访问控制与脱敏：直接标识不外泄，越权访问被明确拒绝且不含原始值。"""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.service import IngestService
from app.store import ClinicalStore

log = logging.getLogger("test.access")

RAW_NAME = "Zhang San"
RAW_ID = "110101199003077777"
RAW_PHONE = "13812345678"

RECORD = json.dumps({
    "patient": {"id": RAW_ID, "identifier": [{"value": RAW_ID}],
                "name": [{"text": RAW_NAME}], "telecom": [{"value": RAW_PHONE}]},
    "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
    "observations": [{"id": "o1", "code": {"coding": [{"code": "GLU"}]},
                      "valueQuantity": {"value": 5.6}}],
})


@pytest.fixture()
def client():
    svc = IngestService(ClinicalStore(), salt="test-salt")
    c = TestClient(create_app(svc))
    r = c.post("/ingest/b1", json={"records": [RECORD]})
    assert r.status_code == 200
    return c


def _patient_key(client) -> str:
    svc = client.app.state.service
    return next(iter(svc._store._patients))


def test_persistence_is_masked(client):
    svc = client.app.state.service
    stored = next(iter(svc._store._patients.values()))
    log.info("输入摘要=含姓名/证件号/手机的记录 持久化=%s", stored)
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in str(stored), f"持久化中泄露了原始值: {raw}"


def test_detail_read_requires_role(client):
    key = _patient_key(client)
    r_anon = client.get(f"/patients/{key}")
    r_wrong = client.get(f"/patients/{key}", headers={"X-Role": "auditor"})
    r_ok = client.get(f"/patients/{key}", headers={"X-Role": "clinician"})
    log.info("输入摘要=明细查询 判定: 匿名=%d auditor=%d clinician=%d",
             r_anon.status_code, r_wrong.status_code, r_ok.status_code)
    assert r_anon.status_code == 401
    assert r_wrong.status_code == 403
    assert r_ok.status_code == 200
    body = json.dumps(r_ok.json())
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in body, "读取路径泄露原始标识"


def test_export_requires_role(client):
    assert client.get("/export").status_code == 401
    r = client.get("/export", headers={"X-Role": "clinician"})
    assert r.status_code == 403
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in r.text, "错误信息中不得出现原始值"
    assert client.get("/export", headers={"X-Role": "auditor"}).status_code == 200


def test_error_message_has_no_raw_values(client):
    bad = json.dumps({"patient": {"id": RAW_ID, "telecom": [{"value": RAW_PHONE}]},
                      "observations": []})
    r = client.post("/ingest/b2", json={"records": [bad]})
    result = r.json()["results"][0]
    log.info("输入摘要=缺 Encounter 的畸形记录 判定=%s", result["outcome"])
    assert result["outcome"] == "rejected"
    fragment = json.dumps(result)
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in fragment, "拒绝片段泄露原始值"


def test_logs_have_no_raw_values(client, caplog):
    key = _patient_key(client)
    with caplog.at_level(logging.INFO):
        client.post("/ingest/b3", json={"records": [RECORD]})
        client.get(f"/patients/{key}", headers={"X-Role": "auditor"})
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in caplog.text, f"日志泄露原始值: {raw}"
