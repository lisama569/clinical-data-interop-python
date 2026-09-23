"""摄取服务：解析 -> 归一 -> 幂等落库 -> 冲突结论。

- 同一批次号重复提交：直接返回首个批次结果的重放标记，不重复落库；
- 单条记录失败不影响批次内其他记录，失败原因按类型区分并保留脱敏片段；
- 同一资源键并发/重复到达且内容不一致：记录为 conflict，不覆盖已有值。
"""
from __future__ import annotations

import logging
import threading

from app.errors import RecordRejectedError
from app.models import BatchResult, RecordOutcome, RecordResult
from app.parsers import parse_record
from app.store import CONFLICT, CREATED, ClinicalStore

logger = logging.getLogger("clinical_interop.ingest")


class IngestService:
    def __init__(self, store: ClinicalStore, salt: str) -> None:
        self._store = store
        self._salt = salt
        self._batch_lock = threading.Lock()
        self._batch_results: dict[str, BatchResult] = {}

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
            result.results.append(self._ingest_one(source_id, payload))

        with self._batch_lock:
            self._batch_results[batch_id] = result
        return result

    def _ingest_one(self, source_id: str, payload: str) -> RecordResult:
        try:
            bundle = parse_record(payload, self._salt)
        except RecordRejectedError as exc:
            logger.info(
                "source=%s 判定=rejected 原因=%s 依据=%s 片段=%s",
                source_id,
                exc.rejection.reason.value,
                exc.rejection.detail,
                exc.rejection.source_fragment,
            )
            return RecordResult(source_id=source_id, outcome=RecordOutcome.REJECTED, rejection=exc.rejection)

        outcomes: list[str] = []
        if bundle.patient is not None:
            outcomes.append(self._store.upsert_patient(bundle.patient))
        if bundle.encounter is not None:
            outcomes.append(self._store.upsert_encounter(bundle.encounter))
        for obs in bundle.observations:
            outcomes.append(self._store.upsert_observation(obs))

        if CONFLICT in outcomes:
            outcome = RecordOutcome.CONFLICT
        elif CREATED in outcomes:
            outcome = RecordOutcome.CREATED
        else:
            outcome = RecordOutcome.DUPLICATE
        logger.info("source=%s 判定=%s 明细=%s", source_id, outcome.value, outcomes)
        return RecordResult(source_id=source_id, outcome=outcome)
