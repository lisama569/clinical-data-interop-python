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
from app.quarantine import QuarantineStore
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
    """并发重放：恰好一个解决者，竞争窗口内的其他触发全部 skipped_busy。

    用两个 Event 做确定性同步：先让唯一的“解决者”进入解析并阻塞，再在它仍占用
    记录时发起其余触发，此时这些触发必然看到 busy；最后放行解决者。结论不依赖
    线程调度时序，因此不会时好时坏。
    """
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
            release.wait(timeout=5)
        return real_parse(payload, salt, registry)

    monkeypatch.setattr(service_mod, "parse_record", slow_parse)

    with ThreadPoolExecutor(max_workers=6) as ex:
        winner = ex.submit(svc.replay_one, pid)
        # 等待解决者确实进入解析（即已持有该记录的重放占用）
        assert entered.wait(timeout=5), "解决者未能进入重放临界区"

        # 解决者仍占用：这些触发必须稳定地得到 skipped_busy
        loser_futs = [ex.submit(svc.replay_one, pid) for _ in range(5)]
        loser_outcomes = [f.result(timeout=5).outcome for f in loser_futs]

        release.set()
        winner_outcome = winner.result(timeout=5).outcome

    assert winner_outcome is ReplayOutcome.RESOLVED, "持锁完成者应解决记录"
    assert set(loser_outcomes) == {ReplayOutcome.SKIPPED_BUSY}, (
        f"占用窗口内的并发触发必须全部 skipped_busy，实际={loser_outcomes}"
    )
    assert svc._store.count_observations() == 1, "并发重放不得产生重复记录"
    log.info("并发重放结论：resolved=1 skipped_busy=%d", len(loser_outcomes))

    # 竞争结束后再次触发：应命中幂等结论，而不是重新处理
    after = svc.replay_one(pid)
    assert after.outcome is ReplayOutcome.ALREADY_RESOLVED
    assert svc._store.count_observations() == 1


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
    # 再来一轮：候选含已解决的 a（重复触发）与仍失败的 b
    batch2 = svc.replay_pending()
    c2 = batch2.counts()
    assert c2.get(ReplayOutcome.STILL_FAILED.value) == 1
    assert c2.get(ReplayOutcome.ALREADY_RESOLVED.value) == 1
    assert ReplayOutcome.RESOLVED.value not in c2  # 本轮没有新解决者
    summary = batch2.summary()
    assert summary == {
        "resolved_now": 0,
        "still_failed": 1,
        "already_resolved": 1,
        "not_processed": 0,
    }
    # 每条候选都有独立结论行，可逐条区分
    by_id = {r.pending_id: r.outcome for r in batch2.results}
    assert by_id[a] is ReplayOutcome.ALREADY_RESOLVED
    assert by_id[b] is ReplayOutcome.STILL_FAILED


def test_batch_replay_distinguishes_four_followup_buckets(svc):
    """一轮批量重放同时区分：本轮解决 / 依然失败 / 此前已解决重复触发 / 本轮未处理。"""
    # will_resolve：本轮将解决；still_bad：依然失败
    will_resolve = _reject_one(
        svc, fhir_unknown("NEW-9", obs_id="ow"), batch="bw"
    )
    still_bad = _reject_one(
        svc, fhir_unknown("STILL-X", obs_id="os"), batch="bs"
    )
    # unhandled：pending 但会因 limit 排到本轮处理之外
    unhandled = _reject_one(
        svc, fhir_unknown("UNHANDLED-X", obs_id="ou"), batch="bu"
    )
    # prior：稍后单独解决，使其在本轮属于“此前已解决、重复触发”
    prior = _reject_one(svc, fhir_unknown("NEW-9", obs_id="op"), batch="bp")

    # 四条都已入区后再扩充字典（避免后续 NEW-9 记录直接入库）
    svc.registry.register_code("9999-1", "New", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")
    assert svc.replay_one(prior).outcome is ReplayOutcome.RESOLVED

    # 候选顺序：pending（will_resolve, still_bad, unhandled）在前，resolved（prior）在后。
    # limit=2：处理前两条 pending；unhandled 超上限 -> not_processed；
    # prior 虽排第 4，但其 already_resolved 结论是幂等查询、不做判定，同样直接给出。
    batch = svc.replay_pending(limit=2)
    assert batch.triggered == 4
    assert batch.processed == 2
    assert batch.limit == 2

    by_id = {r.pending_id: r.outcome for r in batch.results}
    assert by_id[will_resolve] is ReplayOutcome.RESOLVED, "本轮解决"
    assert by_id[still_bad] is ReplayOutcome.STILL_FAILED, "依然失败"
    assert by_id[prior] is ReplayOutcome.ALREADY_RESOLVED, "此前已解决、重复触发"
    assert by_id[unhandled] is ReplayOutcome.NOT_PROCESSED, "本轮还没处理到"

    summary = batch.summary()
    assert summary == {
        "resolved_now": 1,
        "still_failed": 1,
        "already_resolved": 1,
        "not_processed": 1,
    }, summary

    # not_processed 行携带其当前失败类别，便于排期跟进；已解决者无失败类别
    rows = {r.pending_id: r for r in batch.results}
    assert rows[unhandled].reason == RejectReason.UNKNOWN_CODE.value
    assert rows[unhandled].detail
    assert rows[prior].reason is None

    # limit=0：本轮一条 pending 都不实际重放（仍 pending 者全 not_processed），
    # 但已解决记录（含上一轮刚解决的 will_resolve）仍是廉价幂等结论 already_resolved。
    none_batch = svc.replay_pending(limit=0)
    assert none_batch.processed == 0
    assert none_batch.triggered == 4
    none_by_id = {r.pending_id: r.outcome for r in none_batch.results}
    assert none_by_id[still_bad] is ReplayOutcome.NOT_PROCESSED
    assert none_by_id[unhandled] is ReplayOutcome.NOT_PROCESSED
    assert none_by_id[will_resolve] is ReplayOutcome.ALREADY_RESOLVED
    assert none_by_id[prior] is ReplayOutcome.ALREADY_RESOLVED
    assert none_batch.summary()["not_processed"] == 2
    log.info("批量重放四类统计=%s", summary)


def test_batch_replay_resolved_now_bucket(svc):
    """本轮新解决者进入 resolved_now，与此前已解决者不混淆。"""
    a = _reject_one(svc, fhir_unknown("NEW-9", obs_id="a"), batch="ba")
    svc.registry.register_code("9999-1", "New", "mg/dL")
    svc.registry.register_alias("NEW-9", "9999-1")
    batch = svc.replay_pending()
    summary = batch.summary()
    assert summary["resolved_now"] == 1
    assert batch.results[0].ingest_outcome == RecordOutcome.CREATED.value
    # 下一轮：同一条变为 already_resolved
    again = svc.replay_pending()
    assert again.summary() == {
        "resolved_now": 0,
        "still_failed": 0,
        "already_resolved": 1,
        "not_processed": 0,
    }


def test_replay_not_found(svc):
    rr = svc.replay_one("does-not-exist")
    assert rr.outcome is ReplayOutcome.NOT_FOUND


# --------------------------------------------------------------------------
# 容量与淘汰
# --------------------------------------------------------------------------

def test_new_rejection_always_enters_even_when_capacity_full_of_resolved():
    """已解决记录把容量占满时，新的被拒记录仍必须稳稳进入待处理区。

    期望行为：新记录入区成功（不丢失、pending_id 可定位），为此淘汰一条最早的
    resolved 记录转入归档；新记录本身在活动区可查。
    """
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

    # 三条全部重放解决，容量被 resolved 占满
    reg.register_code("9999-1", "New", "mg/dL")
    reg.register_alias("NEW-9", "9999-1")
    for pid in ids:
        assert svc.replay_one(pid).outcome is ReplayOutcome.RESOLVED
    assert svc.quarantine.stats()["total"] == 3

    # 新提交一条必然被拒的记录（仍是未注册编码）：不得丢失
    clock["t"] = 9
    fresh = _reject_one(svc, fhir_unknown("FRESH-9", obs_id="oFresh"), batch="bFresh")

    active = svc.quarantine.get(fresh)
    assert active is not None, "新被拒记录必须进入活动待处理区，不能被悄悄丢掉"
    assert active.status is PendingStatus.PENDING
    assert active.reason is RejectReason.UNKNOWN_CODE

    stats = svc.quarantine.stats()
    assert stats["total"] == 3, "活动区容量仍为上限"
    assert stats["pending"] == 1 and stats["resolved"] == 2
    assert stats["evicted"] == 1, "被挤出的最早 resolved 记录应进入归档"

    # 最早进入的 ids[0] 被淘汰，但仍可追溯
    evicted = svc.quarantine.get(ids[0])
    assert evicted is not None, "被淘汰记录不能像从没存在过一样"
    assert evicted.status is PendingStatus.EVICTED
    assert evicted.evicted_at is not None


def test_capacity_eviction_prefers_oldest_pending_and_is_traceable():
    """容量满且仍有 pending 时淘汰最早 pending；淘汰记录可追溯且类别不被改动。"""
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
    # 解决第一条 -> 它优先于 pending 被保留
    reg.register_code("9999-1", "New", "mg/dL")
    reg.register_alias("NEW-9", "9999-1")
    rr = svc.replay_one(ids[0])
    assert rr.outcome is ReplayOutcome.RESOLVED

    # 第 4 条进入：容量满，应淘汰最早的 pending（ids[1]），resolved 的 ids[0] 保留
    clock["t"] = 9
    fourth = _reject_one(svc, fhir_unknown("OTHER-9", obs_id="oN"), batch="bN")
    assert svc.quarantine.get(ids[0]) is not None, "resolved 记录在有 pending 可淘汰时保留"
    assert fourth is not None

    evicted_rec = svc.quarantine.get(ids[1])
    assert evicted_rec is not None, "被淘汰的 pending 记录必须仍可查到"
    assert evicted_rec.status is PendingStatus.EVICTED
    assert evicted_rec.reason is RejectReason.UNKNOWN_CODE, "淘汰记录保留其失败类别"
    assert evicted_rec.evicted_at == f"2026-01-01T00:00:09+00:00"
    assert evicted_rec.payload == "", "淘汰后原始报文必须清除"
    # 处理历史完整：进入被拒 -> 淘汰事件
    actions = [h.action for h in evicted_rec.history]
    assert actions[-1] is LifecycleAction.EVICTED
    assert any(h.action is LifecycleAction.REJECTED for h in evicted_rec.history)

    # 其余仍在活动区的记录类别不变
    assert svc.quarantine.get(ids[2]).status is PendingStatus.PENDING
    assert svc.quarantine.get(ids[2]).reason is RejectReason.UNKNOWN_CODE

    # 归档记录可通过列表按 status=evicted 过滤出来
    ev_listing = svc.quarantine.list(status=PendingStatus.EVICTED)
    assert ev_listing["total"] == 1
    assert ev_listing["items"][0]["pending_id"] == ids[1]
    assert ev_listing["counts"]["by_status"] == {"evicted": 1}
    # 对外视图不含原始报文字段
    assert "payload" not in ev_listing["items"][0]

    # 归档记录不可再重放：得到明确的 evicted 结论（而非 not_found）
    replay_rr = svc.replay_one(ids[1])
    assert replay_rr.outcome is ReplayOutcome.EVICTED
    assert replay_rr.reason == RejectReason.UNKNOWN_CODE.value


def test_evicted_record_keeps_full_replay_history():
    """被淘汰前若被重放过，其失败类别演变与处理历史必须完整保留。"""
    clock = {"t": 0}

    def c():
        clock["t"] += 1
        return f"2026-01-01T00:00:{clock['t']:02d}+00:00"

    q = QuarantineStore(capacity=1, clock=c)
    reg = CodeRegistry()
    svc = IngestService(ClinicalStore(), salt="test-salt", registry=reg, quarantine=q)

    pid = _reject_one(svc, fhir_unknown())
    # 先重放一次仍失败（unknown_code），留下 replayed 历史
    rr = svc.replay_one(pid)
    assert rr.outcome is ReplayOutcome.STILL_FAILED
    # 容量为 1，下一条被拒进入时淘汰本条
    other = _reject_one(svc, fhir_unknown("OTHER-X", obs_id="o2"), batch="b2")
    assert other is not None

    evicted_rec = svc.quarantine.get(pid)
    assert evicted_rec.status is PendingStatus.EVICTED
    actions = [h.action for h in evicted_rec.history]
    assert actions == [
        LifecycleAction.REJECTED,
        LifecycleAction.REPLAYED,
        LifecycleAction.EVICTED,
    ], actions
    assert evicted_rec.reason is RejectReason.UNKNOWN_CODE
    assert evicted_rec.replay_count == 1, "历史重放次数应保留，可回答是否被重放过"


def test_quarantine_full_error_no_longer_raised_when_all_resolved():
    """旧的“全 resolved 则拒绝入区”语义已改为淘汰最早 resolved 腾出位置。"""
    q = QuarantineStore(capacity=1)
    first, created = q.add_rejected(
        source_id="s0", batch_id="b0", format="unknown",
        reason=RejectReason.MALFORMED_FORMAT, detail="d",
        source_fragment="f", masked_payload="m", payload="p",
        fingerprint="fp0",
    )
    assert created
    only = next(iter(q._records.values()))
    q.acquire(only.pending_id)
    q.resolve(only.pending_id, "2026-01-01T00:00:00+00:00", "created")

    # 再来一条：不抛异常，新记录入区，旧的 resolved 记录被淘汰归档
    second, created2 = q.add_rejected(
        source_id="s1", batch_id="b1", format="unknown",
        reason=RejectReason.UNKNOWN_CODE, detail="d2",
        source_fragment="f2", masked_payload="m2", payload="p2",
        fingerprint="fp1",
    )
    assert created2
    assert q.get(second.pending_id).status is PendingStatus.PENDING
    archived = q.get(only.pending_id)
    assert archived.status is PendingStatus.EVICTED


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


def test_batch_replay_api_reports_four_buckets_and_limit(client):
    """批量重放接口：summary 四类可区分；limit 截断时未处理者逐条可见。"""
    # 三条 unknown：前两条编码将补齐（本轮解决），第三条仍失败；再准备一条未处理
    def ingest(code, obs_id, batch):
        r = client.post(f"/ingest/{batch}",
                        json={"records": [fhir_unknown(code, obs_id=obs_id)]})
        assert r.status_code == 200
        return r.json()["results"][0]["pending_id"]

    p_resolve = ingest("NEW-9", "oa", "ba")
    p_still = ingest("STILL-X", "ob", "bb")
    p_unhandled = ingest("LATER-X", "oc", "bc")

    assert client.post("/admin/codes",
                       json={"code": "9999-1", "display": "New", "unit": "mg/dL"},
                       headers={"X-Role": "admin"}).status_code == 200
    assert client.post("/admin/codes/9999-1/aliases",
                       json={"alias": "NEW-9", "target_code": "9999-1"},
                       headers={"X-Role": "admin"}).status_code == 200

    # 无权限：auditor 不能触发批量重放
    denied = client.post("/quarantine/replay", json={}, headers={"X-Role": "auditor"})
    assert denied.status_code == 403

    # limit=1：只实际重放排第一的 pending（按进入顺序为 p_resolve）
    r = client.post("/quarantine/replay", json={"limit": 1},
                    headers={"X-Role": "admin"})
    assert r.status_code == 200
    body = r.json()
    assert body["triggered"] == 3
    assert body["processed"] == 1
    assert body["summary"] == {
        "resolved_now": 1,
        "still_failed": 0,
        "already_resolved": 0,
        "not_processed": 2,
    }
    by_id = {row["pending_id"]: row["outcome"] for row in body["results"]}
    assert by_id[p_resolve] == ReplayOutcome.RESOLVED.value
    assert by_id[p_still] == ReplayOutcome.NOT_PROCESSED.value
    assert by_id[p_unhandled] == ReplayOutcome.NOT_PROCESSED.value

    # 再来一轮不带 limit：p_resolve 变 already_resolved，其余两条 still_failed
    r2 = client.post("/quarantine/replay", json={}, headers={"X-Role": "admin"})
    s2 = r2.json()["summary"]
    assert s2 == {
        "resolved_now": 0,
        "still_failed": 2,
        "already_resolved": 1,
        "not_processed": 0,
    }

    # limit 必须非负
    bad = client.post("/quarantine/replay", json={"limit": -1},
                      headers={"X-Role": "admin"})
    assert bad.status_code == 422


def test_evicted_record_is_traceable_through_api(client):
    """经 API 端到端：容量淘汰后，被淘汰记录仍可查明细并给出 evicted 结论。"""
    # create_app 由 svc fixture 构建的 store 容量默认 1000，这里单独建小容量应用
    from app.api import create_app as _create_app
    small = IngestService(
        ClinicalStore(),
        salt="test-salt",
        registry=CodeRegistry(),
        quarantine=QuarantineStore(capacity=1),
    )
    c2 = TestClient(_create_app(small))

    r1 = c2.post("/ingest/b1", json={"records": [fhir_unknown(obs_id="o1")]})
    pid = r1.json()["results"][0]["pending_id"]
    # 容量 1：第二条被拒进入时，第一条被淘汰归档
    r2 = c2.post("/ingest/b2", json={"records": [fhir_unknown("X-2", obs_id="o2")]})
    assert r2.json()["results"][0]["pending_id"]

    # 被淘汰记录明细仍可查：200 + status=evicted + 失败类别 + 处理历史
    detail = c2.get(f"/quarantine/records/{pid}", headers={"X-Role": "auditor"})
    assert detail.status_code == 200
    dj = detail.json()
    assert dj["status"] == PendingStatus.EVICTED.value
    assert dj["reason"] == RejectReason.UNKNOWN_CODE.value
    assert dj["evicted_at"]
    assert [h["action"] for h in dj["history"]][-1] == LifecycleAction.EVICTED.value
    assert "payload" not in dj

    # 列表可按 evicted 过滤
    listing = c2.get("/quarantine/records?status=evicted",
                     headers={"X-Role": "auditor"})
    assert listing.json()["total"] == 1
    assert listing.json()["stats"]["evicted"] == 1

    # 对归档记录触发重放：明确 evicted 结论，而不是 404
    replay = c2.post(f"/quarantine/records/{pid}/replay",
                     headers={"X-Role": "admin"})
    assert replay.status_code == 200
    assert replay.json()["outcome"] == ReplayOutcome.EVICTED.value

    # 完全不存在的 id 仍是 404
    missing = c2.post("/quarantine/records/nope/replay",
                      headers={"X-Role": "admin"})
    assert missing.status_code == 404


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
