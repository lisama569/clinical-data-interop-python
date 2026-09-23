"""仓库幂等与并发安全测试。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.models import ConflictVerdict, Observation, Patient
from app.repository import Repository

logger = logging.getLogger("clinical.interop.test")


def _obs(value: float = 5.5, source_id: str = "O1", version: int = 1) -> Observation:
    return Observation(
        source_id=source_id,
        source_system="sysA",
        patient_source_id="P1",
        encounter_source_id=None,
        canonical_code="GLU",
        canonical_value=value,
        canonical_unit="mmol/L",
        effective_time="2026-09-24T08:00:00",
        version=version,
    )


def _patient(name_token: str = "tok-a") -> Patient:
    return Patient(
        source_id="P1",
        source_system="sysA",
        name_token=name_token,
        ident_token="id-tok",
        contact_token="contact-tok",
    )


def test_concurrent_duplicate_inserts_no_duplicate() -> None:
    repo = Repository()
    obs = _obs()
    logger.info("输入摘要: 并发线程数=16, 同一 observation=sysA/O1 判定: 仅一条记录, 其余 IDEMPOTENT_REPLAY")
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: repo.put_observation(obs, None), range(16)))
    verdicts = [v for _, v in results]
    assert verdicts.count(ConflictVerdict.NO_CONFLICT) == 1
    assert verdicts.count(ConflictVerdict.IDEMPOTENT_REPLAY) == 15
    assert len(repo.list_observations()) == 1


def test_concurrent_divergent_updates_without_expected_version() -> None:
    repo = Repository()
    repo.put_observation(_obs(5.5), None)
    logger.info("输入摘要: 并发写入不同数值且不带版本 判定: 无一覆盖, 全部 DIVERGENT_CONTENT")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: repo.put_observation(_obs(6.0 + i * 0.1), None), range(8)))
    assert all(v is ConflictVerdict.DIVERGENT_CONTENT for _, v in results)
    assert repo.get_observation("sysA", "O1").canonical_value == 5.5  # 原值未被覆盖


def test_optimistic_version_conflict() -> None:
    repo = Repository()
    repo.put_observation(_obs(5.5), None)
    # 模拟 A/B 同时基于 v1 编辑
    repo.commit_update.__self__  # noop: 确认仓库可用
    logger.info("输入摘要: A 先提交 v2, B 仍带 expected_version=1 判定: B 收到 VERSION_CONFLICT")
    v1 = repo.get_observation_version("sysA", "O1")
    new_version = repo.commit_update(
        __import__("app.models", fromlist=["ResourceType"]).ResourceType.OBSERVATION,
        "sysA|O1",
        _obs(6.0),
    )
    assert new_version == 2
    data, verdict = repo.put_observation(_obs(7.0), expected_version=v1)
    assert verdict is ConflictVerdict.VERSION_CONFLICT
    assert data.canonical_value == 6.0  # B 的写入未生效


def test_same_content_with_matching_version_is_replay() -> None:
    repo = Repository()
    repo.put_observation(_obs(5.5), None)
    v = repo.get_observation_version("sysA", "O1")
    _, verdict = repo.put_observation(_obs(5.5, version=v), expected_version=v)
    logger.info("输入摘要: 内容与版本完全一致 判定: IDEMPOTENT_REPLAY")
    assert verdict is ConflictVerdict.IDEMPOTENT_REPLAY


def test_batch_fingerprint_idempotency() -> None:
    repo = Repository()
    logger.info("输入摘要: batch=B1 指纹两次相同 判定: 第二次 seen_batch 命中")
    assert repo.seen_batch("B1") is None
    repo.remember_batch("B1", b"fingerprint")
    assert repo.seen_batch("B1") == b"fingerprint"


def test_patient_tokens_are_identifiers() -> None:
    repo = Repository()
    p = _patient()
    repo.put_patient(p, None)
    loaded = repo.get_patient("sysA", "P1")
    logger.info("输入摘要: 患者存储字段 判定: 仅含令牌, 无明文姓名/证件/联系")
    assert loaded.name_token == "tok-a"
    assert not hasattr(loaded, "name")
