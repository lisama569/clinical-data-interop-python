"""待处理区与重放闭环：进入/退出、字典更新后重放、容量淘汰、生命周期。"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.codes import register_alias, register_code
from app.models import RecordOutcome, RejectReason
from app.quarantine import QuarantineStatus, ReplayOutcome
from app.service import IngestService
from app.store import ClinicalStore

log = logging.getLogger("test.quarantine")

RAW_NAME = "Zhang San"
RAW_ID = "110101199003077777"
RAW_PHONE = "13812345678"


def _unknown_code_fhir(code="TSTNEW-1", obs_id="o1", encounter_id="e1", patient_id="p1"):
    return json.dumps({
        "patient": {"id": patient_id, "identifier": [{"value": patient_id}],
                    "name": [{"text": RAW_NAME}], "telecom": [{"value": RAW_PHONE}]},
        "encounter": {"id": encounter_id, "period": {"start": "2026-01-01"}},
        "observations": [{"id": obs_id, "code": {"coding": [{"code": code}]},
                          "valueQuantity": {"value": 5.6, "unit": "mg/dL"}}],
    })


def _missing_field_fhir():
    # 缺 Encounter.period.start
    return json.dumps({
        "patient": {"id": "p2"},
        "encounter": {"id": "e2"},
        "observations": [],
    })


def _service(capacity=1000):
    return IngestService(ClinicalStore(), salt="test-salt", quarantine_capacity=capacity)


def test_rejected_record_enters_quarantine_by_category():
    svc = _service()
    r = svc.ingest_batch("q1", [_unknown_code_fhir()])
    assert r.results[0].outcome is RecordOutcome.REJECTED
    page = svc.quarantine.list_page()
    assert page.total == 1
    entry = page.items[0]
    log.info("被拒条目 entry=%s 类别=%s 状态=%s token=%s",
             entry.entry_id, entry.reason.value, entry.status.value, entry.code_token)
    assert entry.reason is RejectReason.UNKNOWN_CODE
    assert entry.status is QuarantineStatus.PENDING
    assert entry.code_token == "TSTNEW-1"
    assert entry.history[0].action == "admitted"
    # 原始报文绝不随条目视图外泄
    assert entry._payload_raw == ""


def test_replay_after_dictionary_update_resolves_and_persists():
    svc = _service()
    svc.ingest_batch("q2", [_unknown_code_fhir(code="TSTNEW-2")])
    entry = svc.quarantine.list_page().items[0]
    assert svc._store.count_observations() == 0

    # 字典更新前重放：仍失败，类别/依据更新为最新结论，继续留在待处理区
    before = svc.replay_pending(entry.entry_id)
    assert before.outcome is ReplayOutcome.STILL_FAILED
    assert before.reason == RejectReason.UNKNOWN_CODE.value
    entry_after = svc.quarantine.get(entry.entry_id)
    assert entry_after.status is QuarantineStatus.FAILED
    assert entry_after.attempt_count == 1

    # 字典热更新（不重启）：先注册编码再注册别名
    register_code("TSTNEW-2", display="Test analyte", unit="mg/dL")
    replay = svc.replay_pending(entry.entry_id)
    log.info("字典更新后重放 entry=%s 结论=%s 入库=%s",
             entry.entry_id, replay.outcome.value, replay.record_outcome)
    assert replay.outcome is ReplayOutcome.RESOLVED
    assert replay.record_outcome == RecordOutcome.CREATED.value
    assert svc._store.count_observations() == 1
    resolved = svc.quarantine.get(entry.entry_id)
    assert resolved.status is QuarantineStatus.RESOLVED
    assert resolved.history[-1].action == "replayed"
    assert resolved.history[-1].to_status == "resolved"
    assert resolved.history[-1].result == "created"


def test_replay_resolved_is_idempotent_and_duplicates_resolution():
    svc = _service()
    svc.ingest_batch("q3", [_unknown_code_fhir(code="TSTNEW-3")])
    entry_id = svc.quarantine.list_page().items[0].entry_id
    register_code("TSTNEW-3", display="T3", unit="mg/dL")
    first = svc.replay_pending(entry_id)
    assert first.outcome is ReplayOutcome.RESOLVED
    # 再次触发：幂等命中，不重复入库
    again = svc.replay_pending(entry_id)
    assert again.outcome is ReplayOutcome.ALREADY_RESOLVED
    assert svc._store.count_observations() == 1


def test_replay_after_alias_registration_resolves():
    svc = _service()
    # 直接使用别名令牌被拒，注册别名指向已知编码后重放通过
    svc.ingest_batch("q4", [_unknown_code_fhir(code="BRAND-X-CODE", obs_id="ox")])
    entry_id = svc.quarantine.list_page().items[0].entry_id
    register_alias("BRAND-X-CODE", "2345-7")
    replay = svc.replay_pending(entry_id)
    assert replay.outcome is ReplayOutcome.RESOLVED
    obs = next(iter(svc._store._observations.values()))
    assert obs.code == "2345-7"


def test_replay_conflict_matches_single_submit_conclusion():
    """重放入库与单条提交走同一判定：同键不同值 -> conflict，但条目仍标记 resolved。"""
    svc = _service()
    # 先让同一 observation 键以不同值存在
    existing = json.dumps({
        "patient": {"id": "p1"},
        "encounter": {"id": "e1", "period": {"start": "2026-01-01"}},
        "observations": [{"id": "o1", "code": {"coding": [{"code": "GLU"}]},
                          "valueQuantity": {"value": 9.9}}],
    })
    svc.ingest_batch("q5a", [existing])
    # 被拒记录解析成功后会以值 5.6 撞同一 obs 键
    svc.ingest_batch("q5b", [_unknown_code_fhir(code="TSTNEW-5")])
    entry_id = svc.quarantine.list_page().items[0].entry_id
    register_code("TSTNEW-5", display="T5", unit="mg/dL")
    replay = svc.replay_pending(entry_id)
    assert replay.outcome is ReplayOutcome.RESOLVED
    assert replay.record_outcome == RecordOutcome.CONFLICT.value
    stored = next(iter(svc._store._observations.values()))
    assert stored.value == "9.9", "冲突保护：重放不得覆盖已有值"


def test_concurrent_replay_no_duplicates_and_single_resolution():
    svc = _service()
    svc.ingest_batch("q6", [_unknown_code_fhir(code="TSTNEW-6")])
    entry_id = svc.quarantine.list_page().items[0].entry_id
    register_code("TSTNEW-6", display="T6", unit="mg/dL")

    outcomes = []
    barrier = threading.Barrier(8)

    def fire():
        barrier.wait()
        outcomes.append(svc.replay_pending(entry_id).outcome)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: fire(), range(8)))
    log.info("8 并发重放 结论分布=%s",
             {o.value: outcomes.count(o) for o in set(outcomes)})
    assert outcomes.count(ReplayOutcome.RESOLVED) == 1, "只能有一次真正解决"
    assert outcomes.count(ReplayOutcome.ALREADY_RESOLVED) == 7
    assert svc._store.count_observations() == 1, "并发重放不得产生重复记录"


def test_batch_replay_outcomes_are_distinguishable():
    svc = _service()
    svc.ingest_batch("q7", [
        _unknown_code_fhir(code="TSTNEW-7", obs_id="o7"),
        _missing_field_fhir(),
    ])
    # 仅 unknown_code 类会被新编码救回；missing_required_field 不被选中
    register_code("TSTNEW-7", display="T7", unit="mg/dL")
    summary = svc.replay_batch(reason=RejectReason.UNKNOWN_CODE)
    counts = summary["counts"]
    log.info("批量重放结论分布=%s", counts)
    assert counts["resolved"] == 1
    assert counts["not_processed"] == 1  # 缺字段记录未被筛选命中
    page = svc.quarantine.list_page()
    statuses = {e.entry_id: e.status for e in page.items}
    assert any(s is QuarantineStatus.RESOLVED for s in statuses.values())
    assert any(s is QuarantineStatus.PENDING for s in statuses.values())


def test_list_filter_by_category_time_and_pagination():
    svc = _service()
    svc.ingest_batch("q8", [_unknown_code_fhir(obs_id="a"), _missing_field_fhir()])
    assert svc.quarantine.list_page(reason=RejectReason.UNKNOWN_CODE).total == 1
    assert svc.quarantine.list_page(reason=RejectReason.MISSING_REQUIRED_FIELD).total == 1
    page = svc.quarantine.list_page(limit=1, offset=0)
    assert page.total == 2 and len(page.items) == 1 and page.limit == 1
    page2 = svc.quarantine.list_page(limit=1, offset=1)
    assert page.items[0].entry_id != page2.items[0].entry_id
    # 时间范围过滤（ISO 字符串比较）
    assert svc.quarantine.list_page(start="9999-01-01").total == 0


# ---------------------------------------------------------------- 容量与生命周期

class _FakeClock:
    """可注入的单调时钟，保证淘汰/历史时间戳确定。"""

    def __init__(self):
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-01-01T00:00:{self.n:02d}+00:00"


def test_capacity_eviction_is_fifo_and_spares_resolved():
    from app.quarantine import QuarantineStore
    clock = _FakeClock()
    q = QuarantineStore(capacity=3, clock=clock)

    def admit(tag):
        q.admit(batch_id="b", source_id=tag, payload=f"RAW {RAW_PHONE}",
                reason=RejectReason.UNKNOWN_CODE, detail="无法识别的检验编码: X",
                source_fragment=f"frag {RAW_PHONE}", code_token="X", salt="test-salt")

    admit("s1"); admit("s2"); admit("s3")
    # 解决 s2：已解决记录不受淘汰影响，也不占容量
    q.begin_replay("Q00000002")
    q.finish_replay("Q00000002", resolved=True, record_outcome="created")
    admit("s4")  # 容量(2/3)未满 -> 不淘汰
    assert q.get("Q00000001") is not None
    admit("s5")  # 容量达到 3，再进一条 -> 淘汰最老 pending：Q00000001（跳过已解决的 Q2）
    tomb = q.get("Q00000001")
    assert tomb is not None and tomb.status is QuarantineStatus.EVICTED
    assert tomb._payload_raw == "", "淘汰即丢弃原始报文"
    actions = [e.action for e in tomb.history]
    assert actions == ["admitted", "evicted"]
    # s2（已解决）必须仍在
    resolved = q.get("Q00000002")
    assert resolved.status is QuarantineStatus.RESOLVED
    # 淘汰不改变失败类别（墓碑仍保留进入时类别）
    assert tomb.reason is RejectReason.UNKNOWN_CODE
    counts = q.counts()
    log.info("淘汰后统计=%s", counts)
    assert counts["evicted_total"] == 1
    assert counts["resolved"] == 1
    # 已淘汰条目不可再重放
    assert q.begin_replay("Q00000001") is None


def test_evicted_detail_is_410_and_list_excludes_tombstones():
    from app.quarantine import QuarantineStore
    clock = _FakeClock()
    q = QuarantineStore(capacity=1, clock=clock)
    q.admit(batch_id="b", source_id="a", payload="raw",
            reason=RejectReason.UNKNOWN_CODE, detail="d",
            source_fragment="f", code_token="X", salt="test-salt")
    q.admit(batch_id="b", source_id="c", payload="raw2",
            reason=RejectReason.UNKNOWN_CODE, detail="d2",
            source_fragment="f2", code_token="Y", salt="test-salt")
    # 活跃列表不含墓碑
    assert q.list_page().total == 1
    assert q.list_page().items[0].entry_id == "Q00000002"


def test_lifecycle_history_answers_when_why_and_result():
    from app.quarantine import QuarantineStore
    clock = _FakeClock()
    q = QuarantineStore(capacity=10, clock=clock)
    e = q.admit(batch_id="b", source_id="s", payload="raw",
                reason=RejectReason.UNKNOWN_CODE, detail="无法识别: X",
                source_fragment="frag", code_token="X", salt="test-salt")
    claimed = q.begin_replay(e.entry_id)
    q.finish_replay(e.entry_id, resolved=False,
                    reason=RejectReason.UNKNOWN_CODE, detail="仍无法识别: X",
                    salt="test-salt")
    claimed = q.begin_replay(e.entry_id)
    q.finish_replay(e.entry_id, resolved=True, record_outcome="created")
    final = q.get(e.entry_id)
    actions = [(h.action, h.to_status, h.result) for h in final.history]
    log.info("生命周期=%s", actions)
    assert actions == [
        ("admitted", "pending", None),
        ("replayed", "failed", None),
        ("replayed", "resolved", "created"),
    ]
    assert final.attempt_count == 2


def test_eviction_when_all_pending_is_strict_fifo():
    from app.quarantine import QuarantineStore
    clock = _FakeClock()
    q = QuarantineStore(capacity=2, clock=clock)
    for i in range(4):
        q.admit(batch_id="b", source_id=f"s{i}", payload="raw",
                reason=RejectReason.MALFORMED_FORMAT, detail="d",
                source_fragment="f", code_token=None, salt="test-salt")
    # 容量 2、4 条全 pending：Q1、Q2 依次被淘汰，存活为 Q3、Q4
    assert q.get("Q00000001").status is QuarantineStatus.EVICTED
    assert q.get("Q00000002").status is QuarantineStatus.EVICTED
    page = q.list_page()
    assert [e.entry_id for e in page.items] == ["Q00000003", "Q00000004"]
    # 失败类别在淘汰后保持不变
    assert q.get("Q00000001").reason is RejectReason.MALFORMED_FORMAT


def test_replay_still_failed_then_resolve_is_reproducible():
    """同一待处理条目两次重放结论可复现：失败类别不漂移，更新字典后稳定解决。"""
    svc = _service()
    svc.ingest_batch("q9", [_unknown_code_fhir(code="TSTNEW-9")])
    eid = svc.quarantine.list_page().items[0].entry_id
    r1 = svc.replay_pending(eid)
    r2 = svc.replay_pending(eid)
    assert r1.outcome is ReplayOutcome.STILL_FAILED
    assert r2.outcome is ReplayOutcome.STILL_FAILED
    register_code("TSTNEW-9", display="T9", unit="mg/dL")
    r3 = svc.replay_pending(eid)
    assert r3.outcome is ReplayOutcome.RESOLVED


def test_concurrent_single_and_batch_replay_single_resolution_each():
    svc = _service()
    n = 5
    svc.ingest_batch("qc", [
        _unknown_code_fhir(code=f"TCONC-{i}", obs_id=f"oc{i}") for i in range(n)
    ])
    entries = [e.entry_id for e in svc.quarantine.list_page().items]
    for i in range(n):
        register_code(f"TCONC-{i}", display=f"T{i}", unit="mg/dL")

    def single_fire(eid):
        return svc.replay_pending(eid)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = []
        # 每条记录：2 次单条重放 + 1 次批量重放，并发交错
        for eid in entries:
            futs.append(ex.submit(single_fire, eid))
            futs.append(ex.submit(single_fire, eid))
        futs.append(ex.submit(lambda: svc.replay_batch(reason=RejectReason.UNKNOWN_CODE)))
        futs.append(ex.submit(lambda: svc.replay_batch()))
        single_outcomes = [f.result().outcome for f in futs[:2 * n]]
        batch_summaries = [f.result() for f in futs[2 * n:]]

    resolved = single_outcomes.count(ReplayOutcome.RESOLVED)
    log.info("混合并发: 单条 resolved=%d 批量结论=%s",
             resolved, [b["counts"] for b in batch_summaries])
    assert resolved == n, "每条待处理记录只能被真正解决一次"
    assert svc._store.count_observations() == n, "混合并发不得产生重复记录"
    # 所有条目最终 resolved，且历史中 resolved 事件恰好一次
    for eid in entries:
        e = svc.quarantine.get(eid)
        assert e.status is QuarantineStatus.RESOLVED
        assert sum(1 for h in e.history if h.to_status == "resolved") == 1
