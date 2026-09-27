"""FHIR R4 风格 JSON 解析。

支持两种形态：
1. Bundle：{"resourceType": "Bundle", "entry": [{"resource": {...}}, ...]}
2. 扁平对象：{"patient": {...}, "encounter": {...}, "observations": [...]}

必填字段：
- Patient.identifier[0].value（或 id）
- Encounter.id 与 Encounter.period.start
- Observation.code.coding[0].code 与 Observation.valueQuantity.value
"""
from __future__ import annotations

import json
from typing import Any

from app.codes import normalize_code
from app.errors import RecordRejectedError
from app.masking import mask_payload_fragment, pseudonymize
from app.models import (
    Encounter,
    NormalizedBundle,
    Observation,
    Patient,
    Rejection,
    RejectReason,
)


def _reject(reason: RejectReason, detail: str, payload: str, salt: str) -> None:
    fragment = mask_payload_fragment(payload, salt)
    raise RecordRejectedError(Rejection(reason=reason, detail=detail, source_fragment=fragment))


def _require(condition: bool, detail: str, payload: str, salt: str) -> None:
    if not condition:
        _reject(RejectReason.MISSING_REQUIRED_FIELD, detail, payload, salt)


def _flatten(payload_obj: Any, payload: str, salt: str) -> dict[str, Any]:
    if not isinstance(payload_obj, dict):
        _reject(RejectReason.MALFORMED_FORMAT, "顶层必须是 JSON 对象", payload, salt)
    if payload_obj.get("resourceType") == "Bundle":
        resources = [
            e.get("resource", {})
            for e in payload_obj.get("entry", [])
            if isinstance(e, dict)
        ]
        by_type: dict[str, list[dict[str, Any]]] = {}
        for r in resources:
            if isinstance(r, dict) and "resourceType" in r:
                by_type.setdefault(r["resourceType"], []).append(r)
        patients = by_type.get("Patient", [])
        encounters = by_type.get("Encounter", [])
        return {
            "patient": patients[0] if patients else None,
            "encounter": encounters[0] if encounters else None,
            "observations": by_type.get("Observation", []),
        }
    if "patient" in payload_obj or "observations" in payload_obj:
        return {
            "patient": payload_obj.get("patient"),
            "encounter": payload_obj.get("encounter"),
            "observations": payload_obj.get("observations") or [],
        }
    _reject(RejectReason.MALFORMED_FORMAT, "既不是 Bundle 也不是扁平记录对象", payload, salt)
    return {}  # pragma: no cover


def parse_fhir_json(payload: str, salt: str) -> NormalizedBundle:
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        _reject(RejectReason.MALFORMED_FORMAT, "非法 JSON", payload, salt)

    flat = _flatten(obj, payload, salt)

    # --- Patient ---
    p = flat["patient"]
    _require(isinstance(p, dict), "缺少 Patient 资源", payload, salt)
    identifiers = p.get("identifier") or []
    id_value = identifiers[0].get("value") if identifiers and isinstance(identifiers[0], dict) else None
    id_value = id_value or p.get("id")
    _require(bool(id_value), "Patient.identifier[0].value 缺失", payload, salt)
    name = ""
    names = p.get("name") or []
    if names and isinstance(names[0], dict):
        name = names[0].get("text") or " ".join(
            filter(None, [names[0].get("family"), " ".join(names[0].get("given") or [])])
        )
    phone = ""
    telecoms = p.get("telecom") or []
    if telecoms and isinstance(telecoms[0], dict):
        phone = telecoms[0].get("value") or ""
    patient = Patient(
        patient_key=pseudonymize(f"patient:{id_value}", salt),
        masked_name=pseudonymize(name, salt),
        masked_id_number=pseudonymize(str(id_value), salt),
        masked_phone=pseudonymize(str(phone), salt),
    )

    # --- Encounter ---
    e = flat["encounter"]
    _require(isinstance(e, dict), "缺少 Encounter 资源", payload, salt)
    _require(bool(e.get("id")), "Encounter.id 缺失", payload, salt)
    period = e.get("period") or {}
    _require(bool(period.get("start")), "Encounter.period.start 缺失", payload, salt)
    encounter = Encounter(
        encounter_key=pseudonymize(f"encounter:{e['id']}", salt),
        patient_key=patient.patient_key,
        period_start=str(period["start"]),
        encounter_type=str(e.get("class", {}).get("code", "AMB") if isinstance(e.get("class"), dict) else e.get("class", "AMB")),
    )

    # --- Observations ---
    observations: list[Observation] = []
    for i, o in enumerate(flat["observations"]):
        if not isinstance(o, dict):
            _reject(RejectReason.MALFORMED_FORMAT, f"Observation[{i}] 不是对象", payload, salt)
        codings = (o.get("code") or {}).get("coding") or []
        raw_code = codings[0].get("code") if codings and isinstance(codings[0], dict) else None
        _require(bool(raw_code), f"Observation[{i}].code.coding[0].code 缺失", payload, salt)
        code = normalize_code(str(raw_code))
        if code is None:
            raise RecordRejectedError(
                Rejection(
                    reason=RejectReason.UNKNOWN_CODE,
                    detail=f"无法识别的检验编码: {raw_code}",
                    source_fragment=mask_payload_fragment(payload, salt),
                    code_token=str(raw_code).strip().upper(),
                )
            )
        vq = o.get("valueQuantity") or {}
        _require(vq.get("value") is not None, f"Observation[{i}].valueQuantity.value 缺失", payload, salt)
        obs_id = o.get("id") or f"{e['id']}#{i}"
        observations.append(
            Observation(
                observation_key=pseudonymize(f"observation:{obs_id}", salt),
                encounter_key=encounter.encounter_key,
                code=code,
                value=str(vq["value"]),
                unit=str(vq.get("unit", "")),
                effective_time=str(o.get("effectiveDateTime", period["start"])),
            )
        )

    return NormalizedBundle(patient=patient, encounter=encounter, observations=observations)
