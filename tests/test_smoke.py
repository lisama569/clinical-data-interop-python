"""骨架冒烟测试：应用可导入、根路径可访问。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


def test_root() -> None:
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "clinical-data-interop"
