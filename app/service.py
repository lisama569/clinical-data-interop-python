"""摄取服务：解析 -> 归一 -> 幂等落库 -> 冲突结论；被拒记录进入待处理区并支持字典更新后重放。

- 同一批次号重复提交：直接返回首个批次结果的重放标记，不重复落库；
- 单条记录失败不影响批次内其他记录，失败原因按类型区分并保留脱敏片段，同时持久进待处理区；
- 同一资源键并发/重复到达且内容不一致：记录为 conflict，不覆盖已有值；
- 重放按最新字典重新判定：通过则走与单条提交完全一致的入库路径（created/duplicate/conflict
  结论一致、可复现）；仍不通过则更新失败类别与依据、继续留在待处理区；
- 重放总锁串行化同一条目的重复/并发触发：不产生重复记录、不覆盖已解决结论。
"""
from __future__ import annotations

import logging
import threading

from app.errors import RecordRejectedError
from app.models import BatchResult, RecordOutcome, RecordResult, RejectReason
from app.parsers import parse_record
from app.quarantine import (
    QuarantineStatus,
    QuarantineStore,
    ReplayOutcome,
    ReplayResult,
)
from app.store import CONFLICT, CREATED, ClinicalStore

logger = logging.getLogger("clinical_interop.ingest")


class IngestService:
    def __init__(self, store: ClinicalStore, salt: str,
                 quarantine: QuarantineStore | None = None,
                 quarantine_capacity: int = 1000) -> None:
        self._store = store
        self._salt = salt
        self._batch_lock = threading.Lock()
        self._batch_results: dict[str, BatchResult] = {}
        self._quarantine = quarantine or QuarantineStore(capacity=quarantine_capacity)
        # 重放总锁：串行化所有重放，避免并发触发相互覆盖、重复入库
        self._replay_lock = threading.RLock()

    @property
    def quarantine(self) -> QuarantineStore:
        return self._quarantine

    def ingest_batch(self, batch_id: str, records: list[str]) -> BatchResult:
        # 批次级幂等：同一 batch_id 只处理一次，重放返回首个结果
        with self._batch_lock:
            if self._store.check_batch(batch_id):
                first = self._batch_results.get(batch_id)
                if first is not None:
                    logger.info("batch=%s 幂等重放，跳过重复提交", batch_id)
                    return BatchResult(batch_id=batch_id, replayed=True, results=first.results)
            self._store.commit_batch(batch_id)

        result = BatchResult(batch_id=batch_id, replayed=False)
        for idx, payload in enumerate(records):
            source_id = f"{batch_id}#{idx}"
            result.results.append(self._ingest_one(source_id, payload, batch_id=batch_id))

        with self._batch_lock:
            self._batch_results[batch_id] = result
        return result

    def _ingest_one(self, source_id: str, payload: str,
                    batch_id: str | None = None) -> RecordResult:
        try:
            bundle = parse_record(payload, self._salt)
        except RecordRejectedError as exc:
            rj = exc.rejection
            logger.info(
                "source=%s 判定=rejected 原因=%s 依据=%s 片段=%s",
                source_id, rj.reason.value, rj.detail, rj.source_fragment,
            )
            if batch_id is not None:
                entry = self._quarantine.admit(
                    batch_id=batch_id, source_id=source_id, payload=payload,
                    reason=rj.reason, detail=rj.detail,
                    source_fragment=rj.source_fragment, code_token=rj.code_token,
                    salt=self._salt,
                )
                logger.info("source=%s 进入待处理区 entry=%s 类别=%s",
                            source_id, entry.entry_id, rj.reason.value)
            return RecordResult(source_id=source_id, outcome=RecordOutcome.REJECTED, rejection=rj)

        outcome, outcomes = self._persist_bundle(bundle)
        logger.info("source=%s 判定=%s 明细=%s", source_id, outcome.value, outcomes)
        return RecordResult(source_id=source_id, outcome=outcome)

    def _persist_bundle(self, bundle) -> tuple[RecordOutcome, list[str]]:
        """归一结果入库：与单条提交完全一致的 created/duplicate/conflict 结论。"""
        outcomes: list[str] = []
        if bundle.patient is not None:
            outcomes.append(self._store.upsert_patient(bundle.patient))
        if bundle.encounter is not None:
            outcomes.append(self._store.upsert_encounter(bundle.encounter))
        for obs in bundle.observations:
            outcomes.append(self._store.upsert_observation(obs))
        if CONFLICT in outcomes:
            return RecordOutcome.CONFLICT, outcomes
        if CREATED in outcomes:
            return RecordOutcome.CREATED, outcomes
        return RecordOutcome.DUPLICATE, outcomes

    # ------------------------------------------------------------------ 重放

    def replay_pending(self, entry_id: str) -> ReplayResult:
        """按最新字典重放单条待处理记录。并发/重复触发由重放总锁串行化。"""
        with self._replay_lock:
            entry = self._quarantine.begin_replay(entry_id)
            if entry is None:
                logger.info("entry=%s 重放结论=not_found", entry_id)
                return ReplayResult(entry_id=entry_id, outcome=ReplayOutcome.NOT_FOUND)
            if entry.status is QuarantineStatus.RESOLVED:
                logger.info("entry=%s 重放结论=already_resolved（已解决结论受保护）", entry_id)
                return ReplayResult(entry_id=entry_id, outcome=ReplayOutcome.ALREADY_RESOLVED)
            # 日志只出现脱敏片段与 entry/source 标识，不含原始报文
            logger.info("entry=%s source=%s 开始重放 第%d次 类别=%s",
                        entry_id, entry.source_id, entry.attempt_count, entry.reason.value)
            return self._do_replay(entry)

    def _do_replay(self, entry) -> ReplayResult:
        """对一条已领取的待处理条目执行重新判定（调用方持重放总锁）。"""
        try:
            bundle = parse_record(entry._payload_raw, self._salt)
        except RecordRejectedError as exc:
            rj = exc.rejection
            self._quarantine.finish_replay(
                entry.entry_id, resolved=False, reason=rj.reason,
                detail=rj.detail, salt=self._salt,
            )
            logger.info("entry=%s 重放结论=still_failed 类别=%s 依据=%s",
                        entry.entry_id, rj.reason.value, rj.detail)
            return ReplayResult(
                entry_id=entry.entry_id, outcome=ReplayOutcome.STILL_FAILED,
                reason=rj.reason.value, detail=rj.detail,
            )

        outcome, outcomes = self._persist_bundle(bundle)
        self._quarantine.finish_replay(
            entry.entry_id, resolved=True, record_outcome=outcome.value,
        )
        logger.info("entry=%s 重放结论=resolved 入库=%s 明细=%s",
                    entry.entry_id, outcome.value, outcomes)
        return ReplayResult(
            entry_id=entry.entry_id, outcome=ReplayOutcome.RESOLVED,
            record_outcome=outcome.value,
        )

    def replay_batch(self, *, reason: RejectReason | None = None,
                     code_token: str | None = None) -> dict[str, object]:
        """按最新字典批量重放：筛选仍可重放的条目逐条重新判定。

        - 命中筛选的 pending/failed 条目：resolved / still_failed；
        - 已解决条目不会因批量触发而重复处理（不计入本结果）；
        - 未命中筛选的未解决条目计入 not_processed，便于区分四类结果。
        """
        with self._replay_lock:
            selected = self._quarantine.list_actionable(reason=reason, code_token=code_token)
            selected_ids = {e.entry_id for e in selected}
            results: list[ReplayResult] = []
            for entry in selected:
                claimed = self._quarantine.begin_replay(entry.entry_id)
                if claimed is None or claimed.status is QuarantineStatus.RESOLVED:
                    # 极端情况下条目刚被淘汰或已解决：不重复处理
                    continue
                results.append(self._do_replay(claimed))

            counts = {o.value: 0 for o in ReplayOutcome}
            for r in results:
                counts[r.outcome.value] += 1
            # 未被本次筛选命中、仍处于 pending/failed 的条目
            actionable_all = self._quarantine.list_actionable()
            counts[ReplayOutcome.NOT_PROCESSED.value] = sum(
                1 for e in actionable_all if e.entry_id not in selected_ids
            )
            counts[ReplayOutcome.ALREADY_RESOLVED.value] = 0
            counts[ReplayOutcome.NOT_FOUND.value] = 0
            logger.info("批量重放 筛选=%s 结论分布=%s",
                        {"reason": reason.value if reason else None,
                         "code_token": code_token}, counts)
            return {"results": results, "counts": counts}
