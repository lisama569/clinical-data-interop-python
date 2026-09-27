import pytest

from app.codes import reset_registry
from app.service import IngestService
from app.store import ClinicalStore


@pytest.fixture(autouse=True)
def _reset_code_registry():
    """每个用例前后把运行时字典恢复为内置表，避免热注册在测试间串味。"""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture()
def service() -> IngestService:
    return IngestService(ClinicalStore(), salt="test-salt")
