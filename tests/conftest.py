import pytest

from app.service import IngestService
from app.store import ClinicalStore


@pytest.fixture()
def service() -> IngestService:
    return IngestService(ClinicalStore(), salt="test-salt")
