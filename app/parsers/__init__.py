"""多格式解析器：原始负载 -> ParseReport。"""

from __future__ import annotations

from app.models import Format
from app.parsers.base import BaseParser
from app.parsers.csv_parser import CsvParser
from app.parsers.fhir import FhirParser

_PARSERS: dict[Format, type[BaseParser]] = {
    Format.FHIR_JSON: FhirParser,
    Format.CSV: CsvParser,
}


def get_parser(fmt: Format) -> BaseParser:
    """按格式返回解析器实例。"""

    cls = _PARSERS.get(fmt)
    if cls is None:  # pragma: no cover - 枚举已穷尽
        raise ValueError(f"unsupported format: {fmt}")
    return cls()


__all__ = ["BaseParser", "FhirParser", "CsvParser", "get_parser", "Format"]
