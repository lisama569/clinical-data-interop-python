"""待处理区 + 字典更新后重放的闭环行为测试。

覆盖：
- 被拒记录按类别进入待处理区、分页/分类/时间范围查阅、生命周期历史；
- 字典运行时扩充编码/别名后重放：通过入库、仍失败留区并更新最新结论；
- 重复与并发重放：不产生重复记录、不覆盖冲突保护结论、结论可区分；
- 容量上限的确定性淘汰：不淘汰 resolved、不改变其余记录类别；
- 无权限查阅被明确拒绝；姓名/证件号/联系方式在列表、明细、响应与日志中不泄漏。
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.codes import CodeRegistry
from app.masking import mask_sensitive_context
from app.models import (
    LifecycleAction,
    PendingStatus,
    RecordOutcome,
    RejectReason,
    ReplayOutcome,
)
from app.parsers import parse_record
from app.quarantine import QuarantineFullError, QuarantineStore
from app.service import IngestService
from app.store import ClinicalStore

log = logging.getLogger("test.quarantine")

RAW_NAME = "Zhang San"
RAW_ID = "110101199003077777"
RAW_PHONE = "13812345678"


def fhir_unknown(code: str = "NEW-9", *, name=RAW_NAME, obs_id="o1") -> str:
    return json.dumps({
        "patient": {"id": RAW_ID, "identifier": [{"value": RAW_ID}],
                    "name": [{"text": name, "family": "Zhang", "given": ["San"]}],
                    "telecom": [{"value": RAW_PHONE}]},
        "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
        "observations": [{"id": obs_id, "code": {"coding": [{"code": code}]},
                          "valueQuantity": {"value": 1.0, "unit": "mg/dL"}}],
    })


HL7_UNKNOWN = (
    "MSH|^~\\&|LAB|HOSP\r"
    f"PID|1||P123^^^HOSP||{RAW_NAME.replace(' ', '^')}||M|||^^^{RAW_PHONE}\r"
    "OBR|1|ORD1|||panel||20260101100000\r"
    "OBX|1|NM|NEW-9^New analyte||5.6|mg/dL"
)


@pytest.fixture()
def svc() -> IngestService:
    return IngestService(
        ClinicalStore(),
        salt="test-salt",
        registry=CodeRegistry(),
        quarantine=QuarantineStore(),
    )


def _reject_one(svc: IngestService, payload: str, batch="b1") -> str:
    r = svc.ingest_batch(batch, [payload])
    assert r.results[0].outcome is RecordOutcome.REJECTED
    pid = r.results[0].pending_id
    assert pid, "被拒记录应进入待处理区并返回 pending_id"
    return pid


# --------------------------------------------------------------------------
# 进入与查阅
# --------------------------------------------------------------------------

def test_rejected_record_enters_quarantine_by_category(svc):
    pid = _reject_one(svc, fhir_unknown())
    rec = svc.quarantine.get(pid)
    log.info("被拒入区 pending_id=%s 类别=%s 状态=%s", pid[:8], rec.reason.value, rec.status.value)
    assert rec.status is PendingStatus.PENDING
    assert rec.reason is RejectReason.UNKNOWN_CODE
    assert rec.format == "fhir_json"
    assert rec.history[0].action is LifecycleAction.REJECTED


def test_categories_are_distinguishable(svc):
    svc.ingest_batch("b", [
        "<<<not a record>>>",                                   # malformed
        json.dumps({"patient": {"id": "p"}}),                    # missing field
        fhir_unknown(),                                          # unknown code
    ])
    listing = svc.quarantine.list()
    reasons = {item["reason"] for item in listing["items"]}
    assert reasons == {
        RejectReason.MALFORMED_FORMAT.value,
        RejectReason.MISSING_REQUIRED_FIELD.value,
        RejectReason.UNKNOWN_CODE.value,
    }


def test_list_filter_by_status_reason_time_and_pagination(svc):
    # 3 条 unknown_code，2 条其他类别
    svc.ingest_batch("b1", [fhir_unknown(obs_id="a"),
                            fhir_unknown(obs_id="b"),
                            fhir_unknown(obs_id="c"),
                            "<<<garbage>>>",
                            json.dumps({"patient": {"id": "p"}})])
    only_unknown = svc.quarantine.list(reason=RejectReason.UNKNOWN_CODE)
    assert only_unknown["total"] == 3
    assert only_unknown["counts"]["by_reason"] == {"unknown_code": 3}

    page1 = svc.quarantine.list(page=1, page_size=2)
    page2 = svc.quarantine.list(page=2, page_size=2)
    assert len(page1["items"]) == 2 and len(page2["items"]) == 2
    assert page1["pages"] == 3
    ids1 = {i["pending_id"] for i in page1["items"]}
    ids2 = {i["pending_id"] for i in page2["items"]}
    assert ids1.isdisjoint(ids2), "分页不得重复"

    # 时间范围：用一个极宽/极窄窗口验证过滤
    wide = svc.quarantine.list(start="2000-01-01T00:00:00+00:00",
                               end="2100-01-01T00:00:00+00:00")
    assert wide["total"] == 5
    none = svc.quarantine.list(start="2100-01-01T00:00:00+00:00")
    assert none["total"] == 0

    only_pending = svc.quarantine.list(status=PendingStatus.PENDING)
    assert only_pending["total"] == 5


# --------------------------------------------------------------------------
# 字典更新后重放
# --------------------------------------------------------------------------

def test_replay_resolves_after_runtime_code_and_alias_registration(svc):
    pid = _reject_one(svc, fhir_unknown())
    # 重放前：仍失败，留在区内
    before = svc.replay_one(pid)
    assert before.outcome is ReplayOutcome.STILL_FAILED
    assert before.reason == RejectReason.UNKNOWN_CODE.value
    assert svc.quarantine.get(pid).status is PendingStatus.PENDING

    # 不重启服务，运行时扩充编码与别名
    svc.registry.register_code("9999-1", "New analyte", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")

    after = svc.replay_one(pid)
    log.info("字典更新后重放 pending_id=%s 结论=%s 入库=%s",
             pid[:8], after.outcome.value, after.ingest_outcome)
    assert after.outcome is ReplayOutcome.RESOLVED
    assert after.ingest_outcome == RecordOutcome.CREATED.value
    assert svc._store.count_observations() == 1

    rec = svc.quarantine.get(pid)
    assert rec.status is PendingStatus.RESOLVED
    assert rec.resolved_at is not None
    # 生命周期：rejected -> replayed(fail) -> replayed(resolve)
    actions = [h.action for h in rec.history]
    assert actions == [LifecycleAction.REJECTED,
                       LifecycleAction.REPLAYED,
                       LifecycleAction.REPLAYED]
    last = rec.history[-1]
    assert last.ingest_outcome == RecordOutcome.CREATED.value
    assert last.reason is None  # 解决后不再挂失败类别


def test_replay_still_failed_updates_latest_reason(svc):
    # 一条同时缺字段且编码未知的记录：先注册别名解决编码，重放应暴露 missing 字段
    payload = json.dumps({
        "patient": {"id": RAW_ID},
        "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
        "observations": [{"id": "o1", "code": {"coding": [{"code": "NOPE-X"}]}}],
        # 缺 valueQuantity.value
    })
    pid = _reject_one(svc, payload)
    # 解析顺序：编码先判定 -> unknown_code
    assert svc.quarantine.get(pid).reason is RejectReason.UNKNOWN_CODE
    svc.registry.register_code("7777-7", "X", "mg/dL")
    svc.registry.register_alias("NOPE-X", "7777-7")
    rr = svc.replay_one(pid)
    assert rr.outcome is ReplayOutcome.STILL_FAILED
    # 最新结论变化为必填缺失，且记录留在待处理区
    rec = svc.quarantine.get(pid)
    assert rec.status is PendingStatus.PENDING
    assert rec.reason is RejectReason.MISSING_REQUIRED_FIELD
    assert rec.replay_count == 1


def test_hl7_replay_uses_latest_dictionary(svc):
    pid = _reject_one(svc, HL7_UNKNOWN)
    svc.registry.register_code("9999-1", "New analyte", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")
    rr = svc.replay_one(pid)
    assert rr.outcome is ReplayOutcome.RESOLVED
    assert svc._store.count_observations() == 1


def test_replay_duplicate_is_idempotent_no_extra_records(svc):
    pid = _reject_one(svc, fhir_unknown())
    svc.registry.register_code("9999-1", "New analyte", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")
    first = svc.replay_one(pid)
    assert first.outcome is ReplayOutcome.RESOLVED
    again = svc.replay_one(pid)
    third = svc.replay_one(pid)
    assert again.outcome is ReplayOutcome.ALREADY_RESOLVED
    assert third.outcome is ReplayOutcome.ALREADY_RESOLVED
    assert svc._store.count_observations() == 1, "重复重放不得产生重复记录"


def test_replay_concurrent_single_winner_and_distinguishable(svc, monkeypatch):
    import app.service as service_mod
    pid = _reject_one(svc, fhir_unknown())
    svc.registry.register_code("9999-1", "New analyte", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")

    entered = threading.Event()
    release = threading.Event()
    real_parse = service_mod.parse_record

    def slow_parse(payload, salt, registry=None):
        if "NEW-9" in payload:
            entered.set()
            release.wait(timeout=2)
        return real_parse(payload, salt, registry)

    monkeypatch.setattr(service_mod, "parse_record", slow_parse)

    outcomes: list[ReplayOutcome] = []

    def fire():
        outcomes.append(svc.replay_one(pid).outcome)

    busy_holder: dict[str, ReplayOutcome] = {}

    def fire_busy():
        busy_holder["r"] = svc.replay_one(pid).outcome

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fire) for _ in range(6)]
        assert entered.wait(timeout=2)
        done = ex.submit(fire_busy)  # 竞争窗口内：必为 busy
        # 给 busy 调用一点时间完成 acquire 判定
        for _ in range(50):
            if done.done():
                break
            __import__("time").sleep(0.01)
        release.set()
        for f in futs:
            f.result()
        busy_result = busy_holder["r"]

    assert busy_result is ReplayOutcome.SKIPPED_BUSY, "并发重放应被跳过而非重复处理"
    assert outcomes.count(ReplayOutcome.RESOLVED) == 1, "并发下只能有一个解决者"
    assert set(outcomes) <= {ReplayOutcome.RESOLVED, ReplayOutcome.SKIPPED_BUSY}
    assert svc._store.count_observations() == 1
    log.info("并发重放结论分布=%s", {o.value: outcomes.count(o) for o in set(outcomes)})


def test_replay_into_conflict_does_not_overwrite(svc):
    """重放通过后若撞上同键不同值，沿用单条提交的 conflict 语义，不覆盖先到值。"""
    # 先入库一条同 observation 键但不同值的记录
    existing = json.dumps({
        "patient": {"id": RAW_ID},
        "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
        "observations": [{"id": "o1", "code": {"coding": [{"code": "GLU"}]},
                          "valueQuantity": {"value": 9.9, "unit": "mg/dL"}}],
    })
    svc.ingest_batch("existing", [existing])

    pending = fhir_unknown()  # obs id o1，值 1.0，编码 NEW-9 未知
    pid = _reject_one(svc, pending)
    svc.registry.register_code("9999-1", "New analyte", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")
    rr = svc.replay_one(pid)
    assert rr.outcome is ReplayOutcome.RESOLVED
    assert rr.ingest_outcome == RecordOutcome.CONFLICT.value, "冲突保护结论必须保留"
    stored = list(svc._store._observations.values())
    assert all(o.value == "9.9" for o in stored), "重放不得覆盖已有冲突值"
    assert len(stored) == 1


def test_duplicate_rejected_payload_reuses_pending_record(svc):
    p1 = _reject_one(svc, fhir_unknown(), batch="b1")
    r2 = svc.ingest_batch("b2", [fhir_unknown()])
    p2 = r2.results[0].pending_id
    assert p1 == p2, "同一原始报文重复被拒应复用待处理记录，不新增"
    assert svc.quarantine.list()["total"] == 1


def test_batch_replay_mix_of_outcomes(svc):
    a = _reject_one(svc, fhir_unknown("NEW-9", obs_id="a"), batch="ba")
    b = _reject_one(svc, fhir_unknown("STILL-X", obs_id="b"), batch="bb")
    svc.registry.register_code("9999-1", "New", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")
    batch = svc.replay_pending()
    counts = batch.counts()
    assert counts.get(ReplayOutcome.RESOLVED.value) == 1
    assert counts.get(ReplayOutcome.STILL_FAILED.value) == 1
    # 再来一轮：a 已解决，b 仍失败
    batch2 = svc.replay_pending()
    c2 = batch2.counts()
    assert c2.get(ReplayOutcome.STILL_FAILED.value) == 1
    assert ReplayOutcome.RESOLVED.value not in c2  # 候选只含 pending，已解决不再入批


def test_replay_not_found(svc):
    rr = svc.replay_one("does-not-exist")
    assert rr.outcome is ReplayOutcome.NOT_FOUND


# --------------------------------------------------------------------------
# 容量与淘汰
# --------------------------------------------------------------------------

def test_capacity_eviction_is_deterministic_and_keeps_resolved():
    clock = {"t": 0}

    def c():
        return f"2026-01-01T00:00:{clock['t']:02d}+00:00"

    q = QuarantineStore(capacity=3, clock=c)
    reg = CodeRegistry()
    svc = IngestService(ClinicalStore(), salt="test-salt", registry=reg, quarantine=q)

    ids = []
    for i in range(3):
        clock["t"] = i
        ids.append(_reject_one(svc, fhir_unknown(obs_id=f"o{i}"), batch=f"b{i}"))
    # 解决第一条 -> 它不得被淘汰
    reg.register_code("9999-1", "New", "mg/dL")
    reg.register_alias("NEW-9", "9999-1")
    rr = svc.replay_one(ids[0])
    assert rr.outcome is ReplayOutcome.RESOLVED

    # 第 4 条进入（使用另一个仍未知的编码，确保它确实被拒入区）：
    # 容量满，应淘汰最早的 pending（ids[1]），resolved 的 ids[0] 保留
    clock["t"] = 9
    fourth = _reject_one(svc, fhir_unknown("OTHER-9", obs_id="oN"), batch="bN")
    assert svc.quarantine.get(ids[0]) is not None, "resolved 记录不得被淘汰"
    assert svc.quarantine.get(ids[1]) is None, "最早 pending 应被淘汰"
    assert svc.quarantine.get(fourth) is not None
    # 其余记录类别不变
    assert svc.quarantine.get(ids[2]).reason is RejectReason.UNKNOWN_CODE


def test_quarantine_full_when_all_resolved():
    q = QuarantineStore(capacity=1)
    with pytest.raises(QuarantineFullError):
        for i in range(2):
            q.add_rejected(
                source_id=f"s{i}", batch_id=f"b{i}", format="unknown",
                reason=RejectReason.MALFORMED_FORMAT, detail="d",
                source_fragment="f", masked_payload="m", payload="p",
                fingerprint=f"fp{i}",
            )
            # 直接把唯一记录置为 resolved，使下一轮无可淘汰对象
            only = next(iter(q._records.values()))
            q.acquire(only.pending_id)
            q.resolve(only.pending_id, "2026-01-01T00:00:00+00:00", "created")


# --------------------------------------------------------------------------
# API：权限边界与脱敏不泄漏
# --------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402


@pytest.fixture()
def client(svc):
    return TestClient(create_app(svc))


def _ingest_pending(client: TestClient, code: str = "NEW-9") -> str:
    r = client.post("/ingest/b1", json={"records": [fhir_unknown(code)]})
    assert r.status_code == 200
    return r.json()["results"][0]["pending_id"]


def test_quarantine_list_permission_boundary(client):
    pid = _ingest_pending(client)
    # 未认证：明确 401，而不是返回空结果
    r_anon = client.get("/quarantine/records")
    assert r_anon.status_code == 401
    # clinician 无权查阅待处理区：明确 403
    r_clinician = client.get("/quarantine/records", headers={"X-Role": "clinician"})
    assert r_clinician.status_code == 403
    assert "无权" in r_clinician.json()["detail"]
    # auditor / admin 可查阅，且无权限时绝不会得到“看似为空”的结果
    r_auditor = client.get("/quarantine/records", headers={"X-Role": "auditor"})
    assert r_auditor.status_code == 200 and r_auditor.json()["total"] == 1
    r_admin = client.get("/quarantine/records", headers={"X-Role": "admin"})
    assert r_admin.status_code == 200 and r_admin.json()["total"] == 1


def test_quarantine_detail_permission(client):
    pid = _ingest_pending(client)
    assert client.get(f"/quarantine/records/{pid}").status_code == 401
    assert client.get(f"/quarantine/records/{pid}",
                      headers={"X-Role": "clinician"}).status_code == 403
    assert client.get(f"/quarantine/records/{pid}",
                      headers={"X-Role": "auditor"}).status_code == 200
    assert client.get("/quarantine/records/missing",
                      headers={"X-Role": "auditor"}).status_code == 404


def test_replay_and_dictionary_admin_only(client):
    pid = _ingest_pending(client)
    # 重放需要 manage_quarantine：仅 admin
    assert client.post(f"/quarantine/records/{pid}/replay").status_code == 401
    assert client.post(f"/quarantine/records/{pid}/replay",
                       headers={"X-Role": "auditor"}).status_code == 403
    assert client.post(f"/quarantine/records/{pid}/replay",
                       headers={"X-Role": "clinician"}).status_code == 403
    # 字典维护同样仅 admin
    assert client.post("/admin/codes", json={"code": "9999-1"},
                       headers={"X-Role": "auditor"}).status_code == 403

    # admin 扩充字典并重放成功
    assert client.post("/admin/codes",
                       json={"code": "9999-1", "display": "New", "unit": "mg/dL"},
                       headers={"X-Role": "admin"}).status_code == 200
    assert client.post("/admin/codes/9999-1/aliases",
                       json={"alias": "NEW-9", "target_code": "9999-1"},
                       headers={"X-Role": "admin"}).status_code == 200
    r = client.post(f"/quarantine/records/{pid}/replay", headers={"X-Role": "admin"})
    assert r.status_code == 200
    assert r.json()["outcome"] == ReplayOutcome.RESOLVED.value
    # 再次触发：幂等可区分
    r2 = client.post(f"/quarantine/records/{pid}/replay", headers={"X-Role": "admin"})
    assert r2.json()["outcome"] == ReplayOutcome.ALREADY_RESOLVED.value


def test_quarantine_responses_never_leak_raw_identifiers(client):
    pid = _ingest_pending(client)
    for path in ("/quarantine/records", f"/quarantine/records/{pid}"):
        r = client.get(path, headers={"X-Role": "auditor"})
        assert r.status_code == 200
        body = r.text
        for raw in (RAW_NAME, RAW_ID, RAW_PHONE, "Zhang", "San"):
            assert raw not in body, f"{path} 泄露原始值 {raw}"
        assert '"payload"' not in body, "原始报文字段不得出现在查阅响应中"

    # 摄取被拒响应同样脱敏
    r_ingest = client.post("/ingest/b2", json={"records": [fhir_unknown()]})
    body = r_ingest.text
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE, "Zhang", "San"):
        assert raw not in body, "摄取拒绝响应泄露原始值"


def test_quarantine_logs_have_no_raw_values(client, caplog):
    pid = _ingest_pending(client)
    with caplog.at_level(logging.INFO):
        client.post("/ingest/b9", json={"records": [fhir_unknown()]})
        client.get("/quarantine/records", headers={"X-Role": "auditor"})
        client.post(f"/quarantine/records/{pid}/replay", headers={"X-Role": "admin"})
    for raw in (RAW_NAME, RAW_ID, RAW_PHONE, "Zhang", "San"):
        assert raw not in caplog.text, f"日志泄露原始值: {raw}"


def test_masking_helper_covers_fhir_and_hl7():
    fhir = fhir_unknown()
    hl7 = HL7_UNKNOWN
    for text in (fhir, hl7):
        masked = mask_sensitive_context(text, "test-salt")
        for raw in (RAW_NAME, RAW_ID, RAW_PHONE, "Zhang", "San"):
            assert raw not in masked
