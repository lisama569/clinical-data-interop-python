"""HL7 v2 管道分隔报文解析（MSH/PID/OBR/OBX 子集）。

约定：
- 段以 \\r 或 \\n 分隔，字段以 | 分隔；
- MSH 必须为首段；
- PID-3 患者标识（必填），PID-5 姓名，PID-13 电话；
- OBR-2 就诊/医嘱号（必填），OBR-7 观测时间（必填，作为就诊开始）；
- OBX-3 检验编码（必填，取 ^ 前部分），OBX-5 结果值（必填），OBX-6 单位。
"""
from __future__ import annotations

from app.codes import DEFAULT_REGISTRY, CodeRegistry
from app.errors import RecordRejectedError
from app.masking import mask_free_text, pseudonymize
from app.models import (
    Encounter,
    NormalizedBundle,
    Observation,
    Patient,
    Rejection,
    RejectReason,
)


def _reject(reason: RejectReason, detail: str, payload: str, salt: str) -> None:
    fragment = mask_free_text(payload[:300], salt)
    raise RecordRejectedError(Rejection(reason=reason, detail=detail, source_fragment=fragment))


def _segments(payload: str, salt: str) -> list[list[str]]:
    lines = [ln for ln in payload.replace("\r", "\n").split("\n") if ln.strip()]
    if not lines or not lines[0].startswith("MSH|"):
        _reject(RejectReason.MALFORMED_FORMAT, "缺少 MSH 首段或段分隔非法", payload, salt)
    return [ln.split("|") for ln in lines]


def _field(seg: list[str], idx: int) -> str:
    return seg[idx].strip() if idx < len(seg) else ""


def parse_hl7v2(
    payload: str,
    salt: str,
    registry: CodeRegistry | None = None,
) -> NormalizedBundle:
    reg = registry or DEFAULT_REGISTRY
    segs = _segments(payload, salt)
    pid = next((s for s in segs if s[0] == "PID"), None)
    obr = next((s for s in segs if s[0] == "OBR"), None)
    obxs = [s for s in segs if s[0] == "OBX"]

    if pid is None:
        _reject(RejectReason.MISSING_REQUIRED_FIELD, "缺少 PID 段", payload, salt)
    patient_id = _field(pid, 3).split("^")[0]
    if not patient_id:
        _reject(RejectReason.MISSING_REQUIRED_FIELD, "PID-3 患者标识缺失", payload, salt)
    name = _field(pid, 5).replace("^", " ").strip()
    phone = _field(pid, 13).split("^")[0]
    patient = Patient(
        patient_key=pseudonymize(f"patient:{patient_id}", salt),
        masked_name=pseudonymize(name, salt),
        masked_id_number=pseudonymize(patient_id, salt),
        masked_phone=pseudonymize(phone, salt),
    )

    if obr is None:
        _reject(RejectReason.MISSING_REQUIRED_FIELD, "缺少 OBR 段", payload, salt)
    order_id = _field(obr, 2) or _field(obr, 3)
    if not order_id:
        _reject(RejectReason.MISSING_REQUIRED_FIELD, "OBR-2 就诊/医嘱号缺失", payload, salt)
    obs_time = _field(obr, 7)
    if not obs_time:
        _reject(RejectReason.MISSING_REQUIRED_FIELD, "OBR-7 观测时间缺失", payload, salt)
    encounter = Encounter(
        encounter_key=pseudonymize(f"encounter:{order_id}", salt),
        patient_key=patient.patient_key,
        period_start=obs_time,
        encounter_type="LAB",
    )

    observations: list[Observation] = []
    for i, obx in enumerate(obxs):
        raw_code = _field(obx, 3).split("^")[0]
        if not raw_code:
            _reject(RejectReason.MISSING_REQUIRED_FIELD, f"OBX[{i}]-3 检验编码缺失", payload, salt)
        code = reg.normalize_code(raw_code)
        if code is None:
            _reject(RejectReason.UNKNOWN_CODE, f"无法识别的检验编码: {raw_code}", payload, salt)
        value = _field(obx, 5)
        if not value:
            _reject(RejectReason.MISSING_REQUIRED_FIELD, f"OBX[{i}]-5 结果值缺失", payload, salt)
        set_id = _field(obx, 1) or str(i + 1)
        observations.append(
            Observation(
                observation_key=pseudonymize(f"observation:{order_id}#{set_id}", salt),
                encounter_key=encounter.encounter_key,
                code=code,
                value=value,
                unit=_field(obx, 6),
                effective_time=_field(obx, 14) or obs_time,
            )
        )

    return NormalizedBundle(patient=patient, encounter=encounter, observations=observations)
