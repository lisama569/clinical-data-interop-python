"""线程安全的内存存储：幂等键、冲突检测、批次重放。

语义：
- upsert_*：键不存在 -> created；键存在且内容一致 -> duplicate（幂等命中）；
  键存在但内容不一致 -> conflict（保留已有值，不覆盖，调用方得到可区分的冲突结论）。
- 批次：commit_batch 记录的批次号再次提交时视为重放，直接返回首个结果，不产生重复记录。
- 所有公共方法在同一把可重入锁下执行，保证并发安全。
"""
from __future__ import annotations

import threading
from typing import TypeVar

from app.models import Encounter, Observation, Patient

T = TypeVar("T")

CREATED = "created"
DUPLICATE = "duplicate"
CONFLICT = "conflict"


class ConflictError(Exception):
    """同一资源键被并发更新且内容不一致。"""


class ClinicalStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._patients: dict[str, Patient] = {}
        self._encounters: dict[str, Encounter] = {}
        self._observations: dict[str, Observation] = {}
        self._batches: set[str] = set()

    @staticmethod
    def _upsert(table: dict[str, T], key: str, value: T) -> str:
        existing = table.get(key)
        if existing is None:
            table[key] = value
            return CREATED
        if existing == value:
            return DUPLICATE
        return CONFLICT  # 不覆盖已有值

    def upsert_patient(self, patient: Patient) -> str:
        with self._lock:
            return self._upsert(self._patients, patient.patient_key, patient)

    def upsert_encounter(self, encounter: Encounter) -> str:
        with self._lock:
            return self._upsert(self._encounters, encounter.encounter_key, encounter)

    def upsert_observation(self, observation: Observation) -> str:
        with self._lock:
            return self._upsert(self._observations, observation.observation_key, observation)

    def get_patient(self, patient_key: str) -> Patient | None:
        with self._lock:
            return self._patients.get(patient_key)

    def list_observations(self, patient_key: str) -> list[Observation]:
        with self._lock:
            enc_keys = {
                k for k, e in self._encounters.items() if e.patient_key == patient_key
            }
            return [o for o in self._observations.values() if o.encounter_key in enc_keys]

    def count_observations(self) -> int:
        with self._lock:
            return len(self._observations)

    def check_batch(self, batch_id: str) -> bool:
        """批次是否已处理过（幂等重放判定）。"""
        with self._lock:
            return batch_id in self._batches

    def commit_batch(self, batch_id: str) -> None:
        with self._lock:
            self._batches.add(batch_id)
