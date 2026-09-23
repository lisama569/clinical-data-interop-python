"""导入/查询/更新用例编排。

职责：
- 鉴权前置（无权限明确拒绝，错误信息不含任何原始值）；
- 批次幂等（batch_id + 负载指纹）；重放返回 REPLAYED 且不再写入；
- 逐条 upsert，汇总 NO_CONFLICT/IDEMPOTENT_REPLAY/VERSION_CONFLICT/DIVERGENT_CONTENT；
- 所有读取返回脱敏 DTO：患者只给令牌，绝不回显姓名/证件/联系方式明文；
- 被拒原始片段进入隔离区，仅 auditor 可取用。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict

from app.codes import canonicalize
from app.errors import ClinicalInteropError, ConflictError
from app.models import (
    BatchResult,
    ConflictVerdict,
    Format,
    IngestStatus,
    Observation,
    RejectReason,
    Rejection,
    ResourceType,
    Role,
)
from app.parsers import get_parser
from app.repository import Repository
from app.security import require

logger = logging.getLogger("clinical.interop.service")

_BATCH_SEP = b"\x1f"


class IngestionService:
    def __init__(self, repository: Repository | None = None) -> None:
        self.repo = repository or Repository()

    # ---------------- 批次导入 ----------------
    def ingest_batch(
        self,
        *,
        batch_id: str,
        fmt: Format,
        payload: str | bytes,
        role: Role,
    ) -> BatchResult:
        require(role, "ingest:submit")

        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        fingerprint = hashlib.sha256(fmt.value.encode() + _BATCH_SEP + raw).digest()
        existing = self.repo.seen_batch(batch_id)
        if existing is not None:
            if existing == fingerprint:
                stored = tuple(
                    self._quarantine_to_rejection(q) for q in self.repo.list_rejections(batch_id)
                )
                logger.info("批次幂等重放 batch_id=%s verdict=REPLAYED", batch_id)
                return BatchResult(
                    batch_id=batch_id,
                    status=IngestStatus.REPLAYED,
                    accepted_counts={},
                    rejected_count=len(stored),
                    replay=True,
                    rejections=stored,
                )
            # 同 batch_id 不同内容：明确拒绝，不覆盖原批次结论
            from app.errors import BatchInconsistent

            raise BatchInconsistent(f"batch_id {batch_id} reused with different payload")

        parser = get_parser(fmt)
        report = parser.parse(raw)

        # 被拒记录的可定位原始片段进入隔离区；读取受控，日志绝不输出
        raw_fragments = {r.index: self._raw_for_index(fmt, raw, r.index) for r in report.rejections}
        self.repo.save_rejections(batch_id, list(report.rejections), raw_fragments)

        conflicts: list[str] = []
        counts = {"Patient": 0, "Encounter": 0, "Observation": 0}

        for patient in report.patients:
            data, verdict = self.repo.put_patient(patient, None)
            self._record_conflict(conflicts, ResourceType.PATIENT, patient.source_id, verdict)
            if verdict is ConflictVerdict.NO_CONFLICT:
                counts["Patient"] += 1
        for encounter in report.encounters:
            data, verdict = self.repo.put_encounter(encounter, None)
            self._record_conflict(conflicts, ResourceType.ENCOUNTER, encounter.source_id, verdict)
            if verdict is ConflictVerdict.NO_CONFLICT:
                counts["Encounter"] += 1
        for obs in report.observations:
            data, verdict = self.repo.put_observation(obs, None)
            self._record_conflict(conflicts, ResourceType.OBSERVATION, obs.source_id, verdict)
            if verdict is ConflictVerdict.NO_CONFLICT:
                counts["Observation"] += 1

        self.repo.remember_batch(batch_id, fingerprint)

        total_accepted = sum(counts.values())
        if report.rejections and total_accepted:
            status = IngestStatus.PARTIAL
        elif report.rejections:
            status = IngestStatus.ACCEPTED  # 批次被处理（全拒绝也算完成，结论可查）
        else:
            status = IngestStatus.ACCEPTED

        logger.info(
            "批次处理完成 batch_id=%s status=%s accepted=%s rejected=%d conflicts=%d",
            batch_id,
            status.value,
            counts,
            len(report.rejections),
            len(conflicts),
        )
        return BatchResult(
            batch_id=batch_id,
            status=status,
            accepted_counts=counts,
            rejected_count=len(report.rejections),
            replay=False,
            conflicts=tuple(conflicts),
            rejections=tuple(report.rejections),
        )

    @staticmethod
    def _quarantine_to_rejection(q) -> Rejection:
        return Rejection(
            index=q.index,
            locator=q.locator,
            reason=RejectReason(q.reason),
            detail=q.detail,
            masked_preview=q.masked_preview,
        )

    @staticmethod
    def _record_conflict(
        sink: list[str], resource_type: ResourceType, source_id: str, verdict: ConflictVerdict
    ) -> None:
        if verdict is not ConflictVerdict.NO_CONFLICT:
            sink.append(f"{resource_type.value}/{source_id}:{verdict.value}")

    @staticmethod
    def _raw_for_index(fmt: Format, raw: bytes, index: int) -> str:
        """根据解析 index 取回可定位原始片段（失败时返回空串，不影响主流程）。"""

        text = raw.decode("utf-8", errors="replace")
        try:
            if fmt is Format.FHIR_JSON:
                import json

                bundle = json.loads(text)
                entries = bundle.get("entry", [])
                if 0 <= index < len(entries):
                    return json.dumps(entries[index], ensure_ascii=False)
            else:
                import csv
                import io

                rows = list(csv.reader(io.StringIO(text)))
                data_rows = [
                    ",".join(r)
                    for r in rows[1:]
                    if r and any(c.strip() for c in r)
                ]
                if 0 <= index < len(data_rows):
                    return data_rows[index]
        except Exception:  # pragma: no cover - 防御：留存失败不得拖垮导入
            return ""
        return ""

    # ---------------- 读取（均脱敏） ----------------
    def get_patient_view(self, *, source_system: str, source_id: str, role: Role) -> dict:
        require(role, "patient:read")
        patient = self.repo.get_patient(source_system, source_id)
        if patient is None:
            return {}
        # 仅暴露不可逆令牌与非标识属性；不提供任何明文姓名/证件/联系方式
        return {
            "source_system": patient.source_system,
            "source_id": patient.source_id,
            "name_token": patient.name_token,
            "ident_token": patient.ident_token,
            "contact_token": patient.contact_token,
            "birth_date": patient.birth_date,
            "gender": patient.gender,
            "version": patient.version,
        }

    def list_patient_observations(self, *, patient_source_id: str, role: Role) -> list[dict]:
        require(role, "observation:read")
        return [self._obs_dto(o) for o in self.repo.list_observations(patient_source_id)]

    def export_observations(self, *, role: Role) -> list[dict]:
        require(role, "export:observations")
        return [self._obs_dto(o) for o in self.repo.list_observations()]

    @staticmethod
    def _obs_dto(obs: Observation) -> dict:
        return {
            "source_system": obs.source_system,
            "source_id": obs.source_id,
            "patient_source_id": obs.patient_source_id,
            "encounter_source_id": obs.encounter_source_id,
            "code": obs.canonical_code,
            "value": obs.canonical_value,
            "unit": obs.canonical_unit,
            "effective_time": obs.effective_time,
            "status": obs.status,
            "version": obs.version,
        }

    # ---------------- 拒绝清单 / 隔离区 ----------------
    def list_rejections(self, *, batch_id: str | None, role: Role) -> list[dict]:
        require(role, "rejection:list")
        return [self._quarantine_dto(q) for q in self.repo.list_rejections(batch_id)]

    @staticmethod
    def _rejection_dto(r: Rejection) -> dict:
        """解析期拒绝（无 batch_id，由当前批次上下文补充）。"""

        return {
            "index": r.index,
            "locator": r.locator,
            "reason": r.reason.value,
            "detail": r.detail,
            "masked_preview": r.masked_preview,
        }

    @staticmethod
    def _quarantine_dto(q) -> dict:
        """隔离区留存条目。"""

        return {
            "batch_id": q.batch_id,
            "index": q.index,
            "locator": q.locator,
            "reason": q.reason,
            "detail": q.detail,
            "masked_preview": q.masked_preview,
        }

    def get_rejected_fragment(self, *, batch_id: str, index: int, role: Role) -> dict:
        require(role, "rejection:fragment")
        fragment = self.repo.get_raw_fragment(batch_id, index)
        if fragment is None:
            return {}
        import json

        from app.masking import mask_record

        # 即使是审计角色，直接标识也必须脱敏后返回。
        # CSV 原始行为无键纯值，无法可靠对齐表头做遮蔽，按最小暴露原则不回放原文。
        try:
            structured = json.loads(fragment)
            safe_text = json.dumps(mask_record(structured), ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            safe_text = "<redacted: flat-text fragment; see masked_preview>"
        return {"batch_id": batch_id, "index": index, "fragment_masked": safe_text}

    # ---------------- 观测更新（乐观锁） ----------------
    def update_observation(
        self,
        *,
        source_system: str,
        source_id: str,
        value: float,
        unit: str,
        expected_version: int,
        role: Role,
    ) -> tuple[dict, ConflictVerdict]:
        require(role, "observation:update")
        current = self.repo.get_observation(source_system, source_id)
        if current is None:
            raise ClinicalInteropError(f"observation not found: {source_system}/{source_id}")
        current_version = self.repo.get_observation_version(source_system, source_id)
        canonical_code, canonical_value, canonical_unit = canonicalize(
            current.canonical_code, value, unit
        )
        # 先以与现有记录相同的 status 构造候选体，用于"是否真的变化"判定；
        # 只有确实发生变化并提交时，才把 status 标记为 amended。
        candidate = Observation(
            source_id=current.source_id,
            source_system=current.source_system,
            patient_source_id=current.patient_source_id,
            encounter_source_id=current.encounter_source_id,
            canonical_code=current.canonical_code,
            canonical_value=canonical_value,
            canonical_unit=canonical_unit,
            effective_time=current.effective_time,
            status=current.status,
        )
        data, verdict = self.repo.put_observation(candidate, expected_version)
        if verdict is ConflictVerdict.NO_CONFLICT:
            amended = Observation(**{**asdict(candidate), "status": "amended"})
            new_version = self.repo.commit_update(
                ResourceType.OBSERVATION, f"{source_system}|{source_id}", amended
            )
            dto = self._obs_dto(amended)
            dto["version"] = new_version
            return dto, verdict
        if verdict is ConflictVerdict.IDEMPOTENT_REPLAY:
            dto = self._obs_dto(data)
            dto["version"] = current_version
            return dto, verdict
        # VERSION_CONFLICT / DIVERGENT_CONTENT：不覆盖
        raise ConflictError("Observation", source_id, verdict)
