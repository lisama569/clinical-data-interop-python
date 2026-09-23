"""幂等与并发：重复提交不产生重复记录，并发更新给出可区分结论。"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from app.models import RecordOutcome
from app.service import IngestService
from app.store import ClinicalStore

log = logging.getLogger("test.idempotency")


def _rec(patient_id="p1", encounter_id="e1", obs_id="o1", value=5.6):
    return json.dumps({
        "patient": {"id": patient_id},
        "encounter": {"id": encounter_id, "period": {"start": "2026-01-01"}},
        "observations": [{"id": obs_id, "code": {"coding": [{"code": "GLU"}]},
                          "valueQuantity": {"value": value, "unit": "mg/dL"}}],
    })


def _service():
    return IngestService(ClinicalStore(), salt="test-salt")


def test_same_batch_replay_is_idempotent():
    svc = _service()
    r1 = svc.ingest_batch("b1", [_rec()])
    r2 = svc.ingest_batch("b1", [_rec()])
    log.info("输入摘要=同批次重复提交 判定: first=%s replayed=%s",
             r1.results[0].outcome.value, r2.replayed)
    assert r2.replayed is True
    assert svc._store.count_observations() == 1, "重复提交不得产生重复记录"


def test_same_content_new_batch_is_duplicate():
    svc = _service()
    svc.ingest_batch("b1", [_rec()])
    r2 = svc.ingest_batch("b2", [_rec()])
    log.info("输入摘要=新批次同内容 判定=%s", r2.results[0].outcome.value)
    assert r2.results[0].outcome is RecordOutcome.DUPLICATE
    assert svc._store.count_observations() == 1


def test_divergent_update_is_conflict_and_not_overwritten():
    svc = _service()
    svc.ingest_batch("b1", [_rec(value=5.6)])
    r2 = svc.ingest_batch("b2", [_rec(value=9.9)])  # 同一 obs 键，值不同
    log.info("输入摘要=同键不同值 判定=%s", r2.results[0].outcome.value)
    assert r2.results[0].outcome is RecordOutcome.CONFLICT
    stored = svc._store._observations
    assert all(o.value == "5.6" for o in stored.values()), "冲突时不得覆盖已有值"


def test_concurrent_same_batch_no_duplicates():
    svc = _service()
    records = [_rec(obs_id=f"o{i}") for i in range(10)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: svc.ingest_batch("bc", records), range(16)))
    replays = sum(1 for r in results if r.replayed)
    log.info("输入摘要=16 并发同批次 判定: 重放=%d 观测数=%d",
             replays, svc._store.count_observations())
    assert svc._store.count_observations() == 10, "并发重复到达不得产生重复记录"
    assert replays >= 1


def test_concurrent_divergent_updates_single_winner():
    svc = _service()
    def submit(v):
        return svc.ingest_batch(f"b{v}", [_rec(value=v)]).results[0].outcome
    with ThreadPoolExecutor(max_workers=8) as ex:
        outcomes = list(ex.map(submit, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]))
    log.info("输入摘要=8 并发同键不同值 判定分布=%s",
             {o.value: outcomes.count(o) for o in set(outcomes)})
    assert outcomes.count(RecordOutcome.CREATED) == 1, "只能有一个创建者"
    assert set(outcomes) <= {RecordOutcome.CREATED, RecordOutcome.CONFLICT}
    stored = svc._store._observations
    assert len(stored) == 1, "并发更新不得相互覆盖出多份"
