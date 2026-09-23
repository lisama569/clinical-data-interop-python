"""CSV 批量检验记录解析器。

列约定（首行表头，逗号分隔；见 docs/compatibility.md）：

    source_system,patient_id,patient_name,id_number,phone,gender,birth_date,
    observation_id,test_code,value,unit,effective_time,status,encounter_id

- 每行产出（必要时）一个 Patient 与一个 Observation；同一患者多次出现按
  (source_system, patient_id) 幂等去重，观测按 (source_system, observation_id) 去重；
- 表头缺少必填列 -> 整批 MALFORMED；
- 行级问题分别拒绝：缺列/空值=MISSING_FIELD，值非数=INVALID_VALUE，
  未知编码=UNKNOWN_CODE，单位不兼容=UNIT_MISMATCH，同批重复=DUPLICATE_IN_BATCH。
"""

from __future__ import annotations

import csv
import io
import json

from app.codes import canonicalize
from app.errors import RecordRejected
from app.masking import mask_record, tokenize
from app.models import (
    Observation,
    ParseReport,
    Patient,
    RawEntry,
    RejectReason,
)
from app.parsers.base import (
    BaseParser,
    reject,
    require_fields,
    valid_date,
    valid_datetime,
    valid_gender,
    valid_obs_status,
)

REQUIRED_COLUMNS = (
    "source_system",
    "patient_id",
    "patient_name",
    "id_number",
    "phone",
    "observation_id",
    "test_code",
    "value",
    "unit",
    "effective_time",
)
OPTIONAL_COLUMNS = ("gender", "birth_date", "status", "encounter_id")
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# CSV 行 -> Patient 所需字段
_PATIENT_FIELDS = ("source_system", "patient_id", "patient_name", "id_number", "phone")
_OBS_FIELDS = ("observation_id", "test_code", "value", "unit", "effective_time")


class CsvParser(BaseParser):
    format_name = "csv"

    def parse(self, payload: str | bytes) -> ParseReport:
        report = ParseReport()
        text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
        stream = io.StringIO(text)
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            reject(
                report,
                RawEntry(0, "csv:header", text[:200]),
                RejectReason.MALFORMED,
                "empty CSV payload: header row required",
            )
            return report

        header = [col.strip() for col in header]
        missing_cols = [col for col in REQUIRED_COLUMNS if col not in header]
        if missing_cols:
            reject(
                report,
                RawEntry(0, "csv:header", ",".join(header)),
                RejectReason.MALFORMED,
                f"missing required columns: {','.join(missing_cols)}",
            )
            return report

        seen_patients: set[tuple[str, str]] = set()
        seen_observations: set[tuple[str, str]] = set()

        for line_no, row in enumerate(reader, start=2):  # 数据行从第 2 行开始
            if not row or all(not cell.strip() for cell in row):
                continue  # 跳过纯空行
            locator = f"csv:row={line_no}"
            raw = ",".join(row)
            entry = RawEntry(line_no - 2, locator, raw)
            self._parse_row(row, header, entry, report, seen_patients, seen_observations)
        return report

    def _parse_row(
        self,
        row: list[str],
        header: list[str],
        entry: RawEntry,
        report: ParseReport,
        seen_patients: set[tuple[str, str]],
        seen_observations: set[tuple[str, str]],
    ) -> None:
        if len(row) != len(header):
            reject(
                report,
                entry,
                RejectReason.MALFORMED,
                f"column count {len(row)} != header count {len(header)}",
            )
            return
        data: dict[str, str] = {header[i]: row[i].strip() for i in range(len(header))}
        # 按列名脱敏后的结构化预览，供任何拒绝原因复用（纯值行无法按位置遮蔽）
        preview = json.dumps(mask_record(data), ensure_ascii=False)
        try:
            self._build_patient(data, entry, report, seen_patients)
            self._build_observation(data, entry, report, seen_observations)
        except RecordRejected as exc:
            reject(report, entry, exc.reason, exc.detail, preview=preview)

    def _build_patient(
        self,
        data: dict[str, str],
        entry: RawEntry,
        report: ParseReport,
        seen: set[tuple[str, str]],
    ) -> None:
        missing = require_fields(data, _PATIENT_FIELDS)
        if missing:
            raise RecordRejected(
                RejectReason.MISSING_FIELD, f"patient field missing: {missing}", entry.locator
            )
        gender = data.get("gender", "") or None
        if gender and not valid_gender(gender):
            raise RecordRejected(RejectReason.INVALID_VALUE, f"unknown gender: {gender!r}", entry.locator)
        birth_date = data.get("birth_date", "") or None
        if birth_date and not valid_date(birth_date):
            raise RecordRejected(RejectReason.INVALID_VALUE, "birth_date is not ISO yyyy-mm-dd", entry.locator)

        key = (data["source_system"], data["patient_id"])
        if key in seen:
            return  # 同一患者在批次中再次出现：幂等跳过，非拒绝
        seen.add(key)
        report.patients.append(
            Patient(
                source_id=data["patient_id"],
                source_system=data["source_system"],
                name_token=tokenize(data["patient_name"]),
                ident_token=tokenize(data["id_number"]),
                contact_token=tokenize(data["phone"]),
                birth_date=birth_date,
                gender=gender,
            )
        )

    def _build_observation(
        self,
        data: dict[str, str],
        entry: RawEntry,
        report: ParseReport,
        seen: set[tuple[str, str]],
    ) -> None:
        missing = require_fields(data, _OBS_FIELDS)
        if missing:
            raise RecordRejected(
                RejectReason.MISSING_FIELD, f"observation field missing: {missing}", entry.locator
            )
        try:
            numeric = float(data["value"])
        except ValueError:
            raise RecordRejected(
                RejectReason.INVALID_VALUE, f"value is not numeric: {data['value']!r}", entry.locator
            )
        if not valid_datetime(data["effective_time"]):
            raise RecordRejected(
                RejectReason.INVALID_VALUE, "effective_time is not ISO8601", entry.locator
            )
        status = data.get("status", "") or "final"
        if not valid_obs_status(status):
            raise RecordRejected(
                RejectReason.INVALID_VALUE, f"invalid observation status: {status!r}", entry.locator
            )

        canonical_code, canonical_value, canonical_unit = canonicalize(
            data["test_code"], numeric, data["unit"]
        )

        key = (data["source_system"], data["observation_id"])
        if key in seen:
            raise RecordRejected(
                RejectReason.DUPLICATE_IN_BATCH,
                f"observation {data['observation_id']} already seen in this batch",
                entry.locator,
            )
        seen.add(key)
        report.observations.append(
            Observation(
                source_id=data["observation_id"],
                source_system=data["source_system"],
                patient_source_id=data["patient_id"],
                encounter_source_id=data.get("encounter_id") or None,
                canonical_code=canonical_code,
                canonical_value=canonical_value,
                canonical_unit=canonical_unit,
                effective_time=data["effective_time"],
                status=status,
            )
        )
