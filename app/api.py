"""FastAPI 路由：批次摄取、明细查询、导出。所有读取路径只返回脱敏数据。"""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import Role, require
from app.errors import PermissionDeniedError
from app.models import BatchResult
from app.service import IngestService
from app.store import ClinicalStore


class IngestRequest(BaseModel):
    records: list[str]


def _role(x_role: str | None) -> Role:
    try:
        return Role(x_role or "")
    except ValueError:
        raise HTTPException(status_code=401, detail="缺少或非法的角色标识")


def create_app(service: IngestService | None = None) -> FastAPI:
    app = FastAPI(title="clinical-data-interop")
    if service is None:
        service = IngestService(ClinicalStore(), salt="dev-salt")
    app.state.service = service

    @app.exception_handler(PermissionDeniedError)
    async def _on_denied(_req: Request, exc: PermissionDeniedError):
        # 错误信息只含角色与动作，不含任何原始敏感值
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.post("/ingest/{batch_id}", response_model=None)
    def ingest(batch_id: str, body: IngestRequest) -> BatchResult:
        return service.ingest_batch(batch_id, body.records)

    @app.get("/patients/{patient_key}", response_model=None)
    def patient_detail(patient_key: str, x_role: str | None = Header(default=None)):
        require(_role(x_role), "read_detail")
        patient = service._store.get_patient(patient_key)
        if patient is None:
            raise HTTPException(status_code=404, detail="患者不存在")
        observations = service._store.list_observations(patient_key)
        return {"patient": patient, "observations": observations}

    @app.get("/export", response_model=None)
    def export(x_role: str | None = Header(default=None)):
        require(_role(x_role), "export")
        store = service._store
        return {
            "patients": len(store._patients),
            "encounters": len(store._encounters),
            "observations": len(store._observations),
        }

    return app
