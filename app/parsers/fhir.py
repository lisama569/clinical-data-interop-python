"""简化 FHIR Bundle（JSON）解析器。

支持的子集（见 docs/compatibility.md）：
- 顶层 ``{"resourceType": "Bundle", "type": "transaction|collection", "entry": [...]}``；
- entry.resource 支持 Patient / Encounter / Observation；
- 身份标识取自 Patient.identifier（system + value）/ telecom / name；
- Observation.code.coding[].code 经编码目录归一，valueQuantity.value/unit 做单位换算；
- 每个 entry 独立校验：畸形、缺字段、未知编码、单位不兼容、非法值分别拒绝。
"""

from __future__ import annotations

import json
from typing import Any

from app.codes import canonicalize
from app.errors import RecordRejected
from app.masking import tokenize
from app.models import (
    Encounter,
    Observation,
    ParseReport,
    Patient,
    RawEntry,
    RejectReason,
    ResourceType,
)
from app.parsers.base import (
    BaseParser,
    reject,
    require_fields,
    valid_date,
    valid_datetime,
    valid_encounter_status,
    valid_gender,
    valid_obs_status,
)

# 批次内业务键 -> 首次出现的 locator，用于检测同批重复
_REQUIRED_PATIENT = ("source_id", "name_token", "ident_token", "contact_token")


def _identifier(obj: dict[str, Any], system_hint: str) -> tuple[str | None, str | None]:
    """从 identifier 列表中取 (system, value)；取第一个可用项。"""

    for ident in obj.get("identifier", []) or []:
        if not isinstance(ident, dict):
            continue
        value = ident.get("value")
        if value:
            return str(ident.get("system") or system_hint), str(value)
    return None, None


def _first_telecom(obj: dict[str, Any]) -> str:
    for telecom in obj.get("telecom", []) or []:
        if isinstance(telecom, dict) and telecom.get("value"):
            return str(telecom["value"])
    return ""


def _display_name_text(obj: dict[str, Any]) -> str:
    for name in obj.get("name", []) or []:
        if not isinstance(name, dict):
            continue
        if name.get("text"):
            return str(name["text"])
        parts: list[str] = []
        for given in name.get("given", []) or []:
            if given:
                parts.append(str(given))
        if name.get("family"):
            parts.append(str(name["family"]))
        if parts:
            return " ".join(parts)
    return ""


class FhirParser(BaseParser):
    format_name = "fhir-json"

    def parse(self, payload: str | bytes) -> ParseReport:
        report = ParseReport()
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        try:
            bundle = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            reject(
                report,
                RawEntry(0, "bundle", text[:200]),
                RejectReason.MALFORMED,
                f"payload is not valid JSON: {exc.msg if isinstance(exc, json.JSONDecodeError) else 'decode error'}",
            )
            return report
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
            reject(
                report,
                RawEntry(0, "bundle", text[:200]),
                RejectReason.UNSUPPORTED_FORMAT,
                "top-level resource is not a FHIR Bundle",
            )
            return report
        entries = bundle.get("entry")
        if not isinstance(entries, list):
            reject(
                report,
                RawEntry(0, "Bundle.entry", text[:200]),
                RejectReason.MALFORMED,
                "Bundle.entry is missing or not a list",
            )
            return report

        seen: dict[tuple[str, str], str] = {}
        for index, wrapper in enumerate(entries):
            locator = f"Bundle.entry[{index}]"
            raw = json.dumps(wrapper, ensure_ascii=False)
            entry = RawEntry(index, locator, raw)
            self._parse_entry(wrapper, entry, report, seen)
        return report

    def _parse_entry(
        self,
        wrapper: Any,
        entry: RawEntry,
        report: ParseReport,
        seen: dict[tuple[str, str], str],
    ) -> None:
        if not isinstance(wrapper, dict):
            reject(report, entry, RejectReason.MALFORMED, "entry is not an object")
            return
        resource = wrapper.get("resource")
        if not isinstance(resource, dict):
            reject(report, entry, RejectReason.MALFORMED, "entry.resource is missing or not an object")
            return
        rtype = resource.get("resourceType")
        try:
            if rtype == "Patient":
                self._parse_patient(resource, entry, report, seen)
            elif rtype == "Encounter":
                self._parse_encounter(resource, entry, report, seen)
            elif rtype == "Observation":
                self._parse_observation(resource, entry, report, seen)
            else:
                reject(
                    report,
                    entry,
                    RejectReason.UNSUPPORTED_FORMAT,
                    f"unsupported resourceType: {rtype!r}",
                )
        except RecordRejected as exc:
            reject(report, entry, exc.reason, exc.detail, resource_type=ResourceType(rtype) if rtype else None)

    def _dedup_check(
        self,
        resource_type: str,
        source_system: str,
        source_id: str,
        entry: RawEntry,
        report: ParseReport,
        seen: dict[tuple[str, str], str],
    ) -> bool:
        key = (resource_type, f"{source_system}|{source_id}")
        if key in seen:
            reject(
                report,
                entry,
                RejectReason.DUPLICATE_IN_BATCH,
                f"duplicate of earlier entry {seen[key]}",
                resource_type=ResourceType(resource_type),
            )
            return True
        seen[key] = entry.locator
        return False

    def _parse_patient(
        self, resource: dict[str, Any], entry: RawEntry, report: ParseReport, seen: dict
    ) -> None:
        system, source_id = _identifier(resource, "default")
        name = _display_name_text(resource)
        contact = _first_telecom(resource)
        # 证件号：取 identifier 中标记为 ID 的条目；否则用 identifier value
        ident_value = ""
        for ident in resource.get("identifier", []) or []:
            if isinstance(ident, dict) and str(ident.get("type", {}).get("coding", [{}])[0].get("code", "")).upper() in {"IDCARD", "SSN", "MR"}:
                ident_value = str(ident.get("value", ""))
                break
        if not ident_value:
            ident_value = source_id or ""

        check_map = {
            "source_id": source_id or "",
            "name": name,
            "id_number": ident_value,
            "phone": contact,
        }
        missing = require_fields(check_map, tuple(check_map))
        if missing:
            raise RecordRejected(
                RejectReason.MISSING_FIELD,
                f"Patient required field missing: {missing}",
                entry.locator,
            )
        birth_date = resource.get("birthDate")
        if birth_date is not None and not valid_date(str(birth_date)):
            raise RecordRejected(
                RejectReason.INVALID_VALUE, f"birthDate is not ISO yyyy-mm-dd: {birth_date!r}", entry.locator
            )
        gender = resource.get("gender")
        if gender is not None and not valid_gender(str(gender)):
            raise RecordRejected(RejectReason.INVALID_VALUE, f"unknown gender: {gender!r}", entry.locator)
        if self._dedup_check("Patient", system, source_id, entry, report, seen):
            return
        report.patients.append(
            Patient(
                source_id=str(source_id),
                source_system=str(system),
                name_token=tokenize(name),
                ident_token=tokenize(ident_value),
                contact_token=tokenize(contact),
                birth_date=str(birth_date) if birth_date else None,
                gender=str(gender) if gender else None,
            )
        )

    def _parse_encounter(
        self, resource: dict[str, Any], entry: RawEntry, report: ParseReport, seen: dict
    ) -> None:
        system, source_id = _identifier(resource, "default")
        enc_id = source_id or str(resource.get("id") or "")
        missing = require_fields({"source_id": enc_id}, ("source_id",))
        if missing:
            raise RecordRejected(RejectReason.MISSING_FIELD, "Encounter.id/identifier required", entry.locator)
        subject = resource.get("subject") or {}
        patient_ref = ""
        if isinstance(subject, dict):
            patient_ref = str(subject.get("reference", "")).removeprefix("Patient/")
        if not patient_ref:
            raise RecordRejected(RejectReason.MISSING_FIELD, "Encounter.subject.reference required", entry.locator)
        status = resource.get("status")
        if not status or not valid_encounter_status(str(status)):
            raise RecordRejected(
                RejectReason.INVALID_VALUE, f"invalid encounter status: {status!r}", entry.locator
            )
        period = resource.get("period") or {}
        start_time = period.get("start") if isinstance(period, dict) else None
        if start_time and not valid_datetime(str(start_time)):
            raise RecordRejected(RejectReason.INVALID_VALUE, "period.start is not ISO8601", entry.locator)
        if self._dedup_check("Encounter", system or "default", enc_id, entry, report, seen):
            return
        report.encounters.append(
            Encounter(
                source_id=str(enc_id),
                source_system=str(system) if system else "default",
                patient_source_id=str(patient_ref),
                status=str(status),
                start_time=str(start_time) if start_time else None,
                end_time=str(period.get("end")) if isinstance(period, dict) and period.get("end") else None,
            )
        )

    def _parse_observation(
        self, resource: dict[str, Any], entry: RawEntry, report: ParseReport, seen: dict
    ) -> None:
        system, source_id = _identifier(resource, "default")
        obs_id = source_id or str(resource.get("id") or "")
        missing = require_fields({"source_id": obs_id}, ("source_id",))
        if missing:
            raise RecordRejected(RejectReason.MISSING_FIELD, "Observation.id/identifier required", entry.locator)

        subject_ref = str((resource.get("subject") or {}).get("reference", "")).removeprefix("Patient/")
        encounter_obj = resource.get("encounter") or {}
        encounter_ref = ""
        if isinstance(encounter_obj, dict):
            encounter_ref = str(encounter_obj.get("reference", "")).removeprefix("Encounter/")
        if not subject_ref:
            raise RecordRejected(RejectReason.MISSING_FIELD, "Observation.subject.reference required", entry.locator)

        coding = (((resource.get("code") or {}).get("coding")) or [])
        code = ""
        for item in coding:
            if isinstance(item, dict) and item.get("code"):
                code = str(item["code"])
                break
        if not code:
            raise RecordRejected(RejectReason.MISSING_FIELD, "Observation.code.coding[].code required", entry.locator)

        quantity = resource.get("valueQuantity")
        if not isinstance(quantity, dict):
            raise RecordRejected(RejectReason.MISSING_FIELD, "Observation.valueQuantity required", entry.locator)
        raw_value = quantity.get("value")
        raw_unit = quantity.get("unit") or quantity.get("code")
        if raw_value is None or not raw_unit:
            raise RecordRejected(RejectReason.MISSING_FIELD, "valueQuantity.value/unit required", entry.locator)
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            raise RecordRejected(RejectReason.INVALID_VALUE, f"value is not numeric: {type(raw_value).__name__}", entry.locator)

        effective = resource.get("effectiveDateTime") or resource.get("effectiveInstant")
        if not effective:
            raise RecordRejected(RejectReason.MISSING_FIELD, "effectiveDateTime required", entry.locator)
        if not valid_datetime(str(effective)):
            raise RecordRejected(RejectReason.INVALID_VALUE, "effectiveDateTime is not ISO8601", entry.locator)

        status = resource.get("status", "final")
        if not valid_obs_status(str(status)):
            raise RecordRejected(RejectReason.INVALID_VALUE, f"invalid observation status: {status!r}", entry.locator)

        # 编码目录归一（UNKNOWN_CODE / UNIT_MISMATCH 在此产生）
        canonical_code, canonical_value, canonical_unit = canonicalize(code, numeric, str(raw_unit))

        if self._dedup_check("Observation", system, obs_id, entry, report, seen):
            return
        report.observations.append(
            Observation(
                source_id=str(obs_id),
                source_system=str(system) if system else "default",
                patient_source_id=subject_ref,
                encounter_source_id=encounter_ref or None,
                canonical_code=canonical_code,
                canonical_value=canonical_value,
                canonical_unit=canonical_unit,
                effective_time=str(effective),
                status=str(status),
            )
        )
