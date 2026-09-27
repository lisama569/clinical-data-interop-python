"""摄取服务：解析 -> 归一 -> 幂等落库 -> 冲突结论，并管理被拒记录待处理区。

- 同一批次号重复提交：直接返回首个批次结果的重放标记，不重复落库；
- 单条记录失败不影响批次内其他记录，失败记录按类型进入待处理区（脱敏持久化）；
- 同一资源键并发/重复到达且内容不一致：记录为 conflict，不覆盖已有值；
- 字典运行时扩充后，可对待处理区记录触发重放：通过则入库（resolved），
  仍失败则更新最新失败类别并继续留在待处理区（pending）。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.codes import DEFAULT_REGISTRY, CodeRegistry
from app.errors import RecordRejectedError
from app.masking import fingerprint_payload, mask_sensitive_context
from app.models import (
    BatchResult,
    PendingStatus,
    RecordOutcome,
    RecordResult,
    Rejection,
    RejectReason,
    ReplayBatchResult,
    ReplayOutcome,
    ReplayResult,
)
from app.parsers import detect_format, parse_record
from app.quarantine import QuarantineFullError, QuarantineStore
from app.store import CONFLICT, CREATED, ClinicalStore

logger = logging.getLogger("clinical_interop.ingest")


class IngestService:
    def __init__(
        self,
        store: ClinicalStore,
        salt: str,
        registry: CodeRegistry | None = None,
        quarantine: QuarantineStore | None = None,
    ) -> None:
        self._store = store
        self._salt = salt
        self._registry = registry or DEFAULT_REGISTRY
        self._quarantine = quarantine or QuarantineStore()
        self._batch_lock = threading.Lock()
        self._batch_results: dict[str, BatchResult] = {}

    @property
    def registry(self) -> CodeRegistry:
        return self._registry

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
            result.results.append(self._ingest_one(batch_id, source_id, payload))

        with self._batch_lock:
            self._batch_results[batch_id] = result
        return result

    def _ingest_one(self, batch_id: str, source_id: str, payload: str) -> RecordResult:
        try:
            bundle = parse_record(payload, self._salt, self._registry)
        except RecordRejectedError as exc:
            pending_id = self._quarantine_reject(batch_id, source_id, payload, exc)
            logger.info(
                "source=%s 判定=rejected 原因=%s pending_id=%s",
                source_id,
                exc.rejection.reason.value,
                pending_id,
            )
            # 响应中的片段同样使用强脱敏版本，避免姓名等原文外泄
            safe_rejection = Rejection(
                reason=exc.rejection.reason,
                detail=exc.rejection.detail,
                source_fragment=mask_sensitive_context(payload, self._salt)[:300],
            )
            return RecordResult(
                source_id=source_id,
                outcome=RecordOutcome.REJECTED,
                rejection=safe_rejection,
                pending_id=pending_id,
            )

        outcome = self._persist_bundle(bundle)
        logger.info("source=%s 判定=%s", source_id, outcome.value)
        return RecordResult(source_id=source_id, outcome=outcome)

    def _quarantine_reject(
        self,
        batch_id: str,
        source_id: str,
        payload: str,
        exc: RecordRejectedError,
    ) -> str | None:
        """把被拒记录送入待处理区。

        当前容量策略保证总能腾出位置（必要时淘汰最早记录并归档），因此正常路径
        一定返回 pending_id；QuarantineFullError 的兜底分支保留仅为防御未来改动。
        """
        rejection = exc.rejection
        fmt = detect_format(payload)
        # 待处理区上下文统一做姓名+数字标识的强脱敏（遮蔽片段也以其为准）
        masked_payload = mask_sensitive_context(payload, self._salt)
        safe_fragment = masked_payload[:300]
        try:
            record, _created = self._quarantine.add_rejected(
                source_id=source_id,
                batch_id=batch_id,
                format=fmt.value if fmt is not None else "unknown",
                reason=rejection.reason,
                detail=rejection.detail,
                source_fragment=safe_fragment,
                masked_payload=masked_payload,
                payload=payload,
                fingerprint=fingerprint_payload(payload, self._salt),
            )
            return record.pending_id
        except QuarantineFullError:
            logger.warning(
                "source=%s 待处理区已满且全部已解决，本条被拒记录无法入区 原因=%s",
                source_id,
                rejection.reason.value,
            )
            return None

    def _persist_bundle(self, bundle) -> RecordOutcome:
        outcomes: list[str] = []
        if bundle.patient is not None:
            outcomes.append(self._store.upsert_patient(bundle.patient))
        if bundle.encounter is not None:
            outcomes.append(self._store.upsert_encounter(bundle.encounter))
        for obs in bundle.observations:
            outcomes.append(self._store.upsert_observation(obs))

        if CONFLICT in outcomes:
            return RecordOutcome.CONFLICT
        if CREATED in outcomes:
            return RecordOutcome.CREATED
        return RecordOutcome.DUPLICATE

    # -- 重放 ---------------------------------------------------------------
    def replay_one(self, pending_id: str) -> ReplayResult:
        """对单条待处理记录按最新字典重新判定。结论可区分、幂等、并发安全。"""
        state, record = self._quarantine.acquire(pending_id)
        if state == "not_found":
            logger.info("pending_id=%s 重放=not_found", pending_id)
            return ReplayResult(pending_id=pending_id, outcome=ReplayOutcome.NOT_FOUND)
        if state == "evicted":
            logger.info(
                "pending_id=%s 重放=evicted（已被容量淘汰归档，不可重放）", pending_id
            )
            return ReplayResult(
                pending_id=pending_id,
                outcome=ReplayOutcome.EVICTED,
                reason=record.reason.value,
                detail="记录已被容量淘汰，仅保留可追溯信息，不能再重放",
            )
        if state == "already_resolved":
            logger.info("pending_id=%s 重放=already_resolved（幂等命中）", pending_id)
            return ReplayResult(pending_id=pending_id, outcome=ReplayOutcome.ALREADY_RESOLVED)
        if state == "busy":
            logger.info("pending_id=%s 重放=skipped_busy（并发跳过）", pending_id)
            return ReplayResult(pending_id=pending_id, outcome=ReplayOutcome.SKIPPED_BUSY)

        # state == "acquired"：本线程负责完成判定，任何路径都必须释放占用
        try:
            return self._do_replay(record)
        except Exception:
            self._quarantine.release(pending_id)
            raise

    def _do_replay(self, record) -> ReplayResult:
        at = datetime.now(timezone.utc).isoformat()
        try:
            bundle = parse_record(record.payload, self._salt, self._registry)
        except RecordRejectedError as exc:
            rejection = exc.rejection
            self._quarantine.refail(
                record.pending_id,
                at,
                reason=rejection.reason,
                detail=rejection.detail,
                source_fragment=mask_sensitive_context(record.payload, self._salt)[:300],
                masked_payload=mask_sensitive_context(record.payload, self._salt),
            )
            logger.info(
                "pending_id=%s 重放=still_failed 原因=%s",
                record.pending_id,
                rejection.reason.value,
            )
            return ReplayResult(
                pending_id=record.pending_id,
                outcome=ReplayOutcome.STILL_FAILED,
                reason=rejection.reason.value,
                detail=rejection.detail,
            )

        # 判定通过：走与单条提交完全一致的 upsert 语义
        outcome = self._persist_bundle(bundle)
        self._quarantine.resolve(record.pending_id, at, outcome.value)
        logger.info(
            "pending_id=%s 重放=resolved 入库结论=%s",
            record.pending_id,
            outcome.value,
        )
        return ReplayResult(
            pending_id=record.pending_id,
            outcome=ReplayOutcome.RESOLVED,
            ingest_outcome=outcome.value,
        )

    def replay_pending(
        self,
        reason: RejectReason | None = None,
        *,
        limit: int | None = None,
    ) -> ReplayBatchResult:
        """对待处理区记录批量重放（可按失败类别限定、可限制本轮处理条数）。

        候选 = pending 记录（reason 过滤）+ resolved 记录（此前已解决者），顺序
        上 pending 优先。对每条候选都给出独立结论，从而区分四种跟进情况：

        - resolved：本轮重放通过并入库；
        - still_failed：依然失败，继续留在待处理区；
        - already_resolved：此前就已解决，本轮属于重复触发；
        - not_processed：本轮还没处理到（超出 ``limit``，或并发占用被跳过）。

        逐条复用 replay_one，因此同样幂等且并发安全。
        """
        targets = self._quarantine.replay_targets(reason)
        if limit is not None:
            limit = max(0, int(limit))
        batch = ReplayBatchResult(triggered=len(targets), limit=limit)
        pending_processed = 0
        for record in targets:
            # limit 只约束需要真正重新解析判定的 pending 记录；
            # resolved 记录是廉价的幂等查询，始终给出 already_resolved。
            if (
                record.status is PendingStatus.PENDING
                and limit is not None
                and pending_processed >= limit
            ):
                batch.results.append(
                    ReplayResult(
                        pending_id=record.pending_id,
                        outcome=ReplayOutcome.NOT_PROCESSED,
                        reason=record.reason.value,
                        detail="本轮批量重放未处理到（超出本轮处理上限），留待后续批次",
                    )
                )
                continue
            if record.status is PendingStatus.PENDING:
                pending_processed += 1
            batch.results.append(self.replay_one(record.pending_id))
        batch.processed = pending_processed
        logger.info(
            "批量重放 候选=%d 实际处理=%d 四类统计=%s 细粒度=%s",
            batch.triggered,
            batch.processed,
            batch.summary(),
            batch.counts(),
        )
        return batch
