"""待处理区 API：权限边界（越权明确拒绝而非空结果）与全链路脱敏。"""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.codes import register_code
from app.service import IngestService
from app.store import ClinicalStore

log = logging.getLogger("test.quarantine.api")

RAW_NAME = "Zhang San"
RAW_ID = "110101199003077777"
RAW_PHONE = "13812345678"

RECORD = json.dumps({
    "patient": {"id": RAW_ID, "identifier": [{"value": RAW_ID}],
                "name": [{"text": RAW_NAME}], "telecom": [{"value": RAW_PHONE}]},
    "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
    "observations": [{"id": "o1", "code": {"coding": [{"code": "APITOK-1"}]},
                      "valueQuantity": {"value": 5.6}}],
})


@pytest.fixture()
def client():
    svc = IngestService(ClinicalStore(), salt="test-salt")
    c = TestClient(create_app(svc))
    r = c.post("/ingest/api-b1", json={"records": [RECORD]})
    assert r.status_code == 200
    assert r.json()["results"][0]["outcome"] == "rejected"
    return c


def _entry_id(client) -> str:
    return client.app.state.service.quarantine.list_page().items[0].entry_id


def test_quarantine_list_requires_permission_not_empty_result(client):
    # 无角色：401；clinician 无查阅权限：403（而不是返回空列表）
    assert client.get("/quarantine").status_code == 401
    denied = client.get("/quarantine", headers={"X-Role": "clinician"})
    assert denied.status_code == 403
    assert "无权" in denied.json()["detail"]
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in denied.text
    # 有权限可见
    ok = client.get("/quarantine", headers={"X-Role": "auditor"})
    assert ok.status_code == 200
    assert ok.json()["total"] == 1


def test_quarantine_detail_requires_permission(client):
    eid = _entry_id(client)
    assert client.get(f"/quarantine/{eid}",
                      headers={"X-Role": "clinician"}).status_code == 403
    assert client.get(f"/quarantine/{eid}",
                      headers={"X-Role": "admin"}).status_code == 200


def test_quarantine_views_never_leak_raw_pii(client):
    eid = _entry_id(client)
    r = client.get(f"/quarantine/{eid}", headers={"X-Role": "auditor"})
    body = r.text
    log.info("待处理明细响应长度=%d", len(body))
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in body, f"待处理区视图泄露原始值: {raw}"
    assert "_payload_raw" not in body, "原始报文字段不得出现在响应中"
    # 列表同样无泄漏
    lst = client.get("/quarantine", headers={"X-Role": "admin"}).text
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in lst


def test_replay_endpoint_permissions(client):
    eid = _entry_id(client)
    # auditor 可查但不可重放
    assert client.post(f"/quarantine/replay/{eid}",
                       headers={"X-Role": "auditor"}).status_code == 403
    assert client.post(f"/quarantine/replay/{eid}",
                       headers={"X-Role": "clinician"}).status_code == 403
    # 未注册编码时 admin 重放 -> still_failed
    r = client.post(f"/quarantine/replay/{eid}", headers={"X-Role": "admin"})
    assert r.status_code == 200 and r.json()["outcome"] == "still_failed"


def test_admin_register_code_then_replay_resolves(client):
    eid = _entry_id(client)
    # 热更新字典
    reg = client.post("/admin/codes", headers={"X-Role": "admin"},
                      json={"code": "APITOK-1", "display": "API analyte", "unit": "mg/dL"})
    assert reg.status_code == 200 and reg.json()["code"] == "APITOK-1"
    # 非 admin 不能改字典
    assert client.post("/admin/codes", headers={"X-Role": "auditor"},
                       json={"code": "X"}).status_code == 403
    r = client.post(f"/quarantine/replay/{eid}", headers={"X-Role": "admin"})
    assert r.json()["outcome"] == "resolved"
    assert r.json()["record_outcome"] == "created"
    # 重复触发 -> already_resolved
    again = client.post(f"/quarantine/replay/{eid}", headers={"X-Role": "admin"})
    assert again.json()["outcome"] == "already_resolved"


def test_batch_replay_endpoint_and_counts(client):
    r = client.post("/quarantine/replay", headers={"X-Role": "admin"},
                    json={"reason": "unknown_code"})
    assert r.status_code == 200
    assert r.json()["counts"]["still_failed"] == 1
    register_code("APITOK-1", display="d", unit="u")
    r2 = client.post("/quarantine/replay", headers={"X-Role": "admin"},
                     json={"reason": "unknown_code"})
    assert r2.json()["counts"]["resolved"] == 1


def test_quarantine_logs_have_no_raw_pii(client, caplog):
    eid = _entry_id(client)
    with caplog.at_level(logging.INFO):
        client.post(f"/quarantine/replay/{eid}", headers={"X-Role": "admin"})
        client.get("/quarantine", headers={"X-Role": "clinician"})
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE):
        assert raw not in caplog.text, f"日志泄露原始值: {raw}"


def test_evicted_entry_returns_410_and_missing_returns_404():
    from app.models import RejectReason
    svc = IngestService(ClinicalStore(), salt="test-salt", quarantine_capacity=1)
    c = TestClient(create_app(svc))
    c.post("/ingest/cap1", json={"records": [RECORD]})
    second = json.loads(RECORD)
    second["observations"][0]["code"]["coding"][0]["code"] = "OTHERTOK-2"
    c.post("/ingest/cap2", json={"records": [json.dumps(second)]})
    # 容量 1：第一条（Q00000001）已被 FIFO 淘汰
    assert c.get("/quarantine/Q00000001",
                 headers={"X-Role": "admin"}).status_code == 410
    assert c.post("/quarantine/replay/Q00000001",
                  headers={"X-Role": "admin"}).status_code == 404
    assert c.get("/quarantine/Q99999999",
                 headers={"X-Role": "admin"}).status_code == 404
