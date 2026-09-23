"""内存持久化仓库。

并发安全：
- 所有读写在同一把可重入锁内完成；
- 资源带单调 ``version``，upsert 采用乐观并发控制，给出可区分结论：
  NO_CONFLICT / IDEMPOTENT_REPLAY / VERSION_CONFLICT / DIVERGENT_CONTENT。

隐私：
- 患者直接标识在进入解析阶段时即被令牌化，仓库中不存在明文姓名/证件/联系方式；
- 被拒绝记录的原始片段进入隔离区，默认读取路径不可见，仅审计接口可按定位取用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Union

from app.models import ConflictVerdict, Encounter, Observation, Patient, Rejection, ResourceType

Record = Union[Patient, Encounter, Observation]


@dataclass
class StoredRecord:
    resource_type: ResourceType
    key: str
    data: Record
    version: int = 1


@dataclass
class QuarantineEntry:
    """拒绝记录留存：原始片段进入隔离区，默认读取路径不可见。"""

    batch_id: str
    index: int
    locator: str
    reason: str
    detail: str
    masked_preview: str
    raw_fragment: str  # 仅 auditor 经审计接口可取用；日志永不输出它


class Repository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[ResourceType, str], StoredRecord] = {}
        self._batch_fingerprints: dict[str, bytes] = {}
        self._rejections: dict[str, list[QuarantineEntry]] = {}

    # ---------------- 批次幂等 ----------------
    def seen_batch(self, batch_id: str) -> bytes | None:
        with self._lock:
            return self._batch_fingerprints.get(batch_id)

    def remember_batch(self, batch_id: str, fingerprint: bytes) -> None:
        with self._lock:
            self._batch_fingerprints[batch_id] = fingerprint

    # ---------------- 资源 upsert（乐观并发） ----------------
    def _put(
        self,
        resource_type: ResourceType,
        key: str,
        incoming: Record,
        expected_version: int | None,
    ) -> tuple[Record, ConflictVerdict]:
        with self._lock:
            stored = self._records.get((resource_type, key))
            if stored is None:
                self._records[(resource_type, key)] = StoredRecord(resource_type, key, incoming, 1)
                return incoming, ConflictVerdict.NO_CONFLICT

            # 乐观锁：调用方先声明自己基于的版本，过期即判定版本冲突
            if expected_version is not None and expected_version != stored.version:
                return stored.data, ConflictVerdict.VERSION_CONFLICT

            if incoming == stored.data:
                return stored.data, ConflictVerdict.IDEMPOTENT_REPLAY

            if expected_version is not None:
                # 显式编辑路径：版本匹配即允许，返回 NO_CONFLICT，由调用方提交新版本
                return incoming, ConflictVerdict.NO_CONFLICT

            # 导入路径（无版本号）且内容不同：拒绝静默相互覆盖
            return stored.data, ConflictVerdict.DIVERGENT_CONTENT

    def put_patient(
        self, patient: Patient, expected_version: int | None
    ) -> tuple[Patient, ConflictVerdict]:
        return self._put(
            ResourceType.PATIENT,
            f"{patient.source_system}|{patient.source_id}",
            patient,
            expected_version,
        )

    def put_encounter(
        self, encounter: Encounter, expected_version: int | None
    ) -> tuple[Encounter, ConflictVerdict]:
        return self._put(
            ResourceType.ENCOUNTER,
            f"{encounter.source_system}|{encounter.source_id}",
            encounter,
            expected_version,
        )

    def put_observation(
        self, obs: Observation, expected_version: int | None
    ) -> tuple[Observation, ConflictVerdict]:
        return self._put(
            ResourceType.OBSERVATION,
            f"{obs.source_system}|{obs.source_id}",
            obs,
            expected_version,
        )

    def commit_update(
        self, resource_type: ResourceType, key: str, data: Record
    ) -> int:
        """在调用方完成冲突校验后提交更新，返回新版本号。"""

        with self._lock:
            stored = self._records[(resource_type, key)]
            new_version = stored.version + 1
            self._records[(resource_type, key)] = StoredRecord(resource_type, key, data, new_version)
            return new_version

    # ---------------- 读取 ----------------
    def get_patient(self, source_system: str, source_id: str) -> Patient | None:
        with self._lock:
            stored = self._records.get((ResourceType.PATIENT, f"{source_system}|{source_id}"))
            return stored.data if stored else None

    def get_observation(self, source_system: str, source_id: str) -> Observation | None:
        with self._lock:
            stored = self._records.get((ResourceType.OBSERVATION, f"{source_system}|{source_id}"))
            return stored.data if stored else None

    def get_observation_version(self, source_system: str, source_id: str) -> int | None:
        with self._lock:
            stored = self._records.get((ResourceType.OBSERVATION, f"{source_system}|{source_id}"))
            return stored.version if stored else None

    def list_observations(self, patient_source_id: str | None = None) -> list[Observation]:
        with self._lock:
            return [
                rec.data
                for rec in self._records.values()
                if rec.resource_type is ResourceType.OBSERVATION
                and (patient_source_id is None or rec.data.patient_source_id == patient_source_id)
            ]

    # ---------------- 拒绝留存（隔离区） ----------------
    def save_rejections(
        self,
        batch_id: str,
        rejections: list[Rejection],
        raw_fragments: dict[int, str],
    ) -> None:
        with self._lock:
            entries = self._rejections.setdefault(batch_id, [])
            for rej in rejections:
                entries.append(
                    QuarantineEntry(
                        batch_id=batch_id,
                        index=rej.index,
                        locator=rej.locator,
                        reason=rej.reason.value,
                        detail=rej.detail,
                        masked_preview=rej.masked_preview,
                        raw_fragment=raw_fragments.get(rej.index, ""),
                    )
                )

    def list_rejections(self, batch_id: str | None = None) -> list[QuarantineEntry]:
        with self._lock:
            if batch_id is not None:
                return list(self._rejections.get(batch_id, ()))
            return [entry for entries in self._rejections.values() for entry in entries]

    def get_raw_fragment(self, batch_id: str, index: int) -> str | None:
        with self._lock:
            for entry in self._rejections.get(batch_id, ()):
                if entry.index == index:
                    return entry.raw_fragment
            return None

    def reset(self) -> None:
        """仅供测试。"""

        with self._lock:
            self._records.clear()
            self._batch_fingerprints.clear()
            self._rejections.clear()
