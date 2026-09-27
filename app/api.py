"""FastAPI 路由：批次摄取、明细查询、导出、待处理区查阅/重放、字典维护。

所有读取路径只返回脱敏数据；待处理区视图一律经过 PendingRecord.to_view()，
原始报文（payload）永不出现在响应、错误信息与日志中。
"""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import Role, require
from app.codes import CodeRegistry, UnknownTargetCodeError
from app.errors import PermissionDeniedError
from app.models import BatchResult, PendingStatus, RejectReason
from app.quarantine import QuarantineStore
from app.service import IngestService
from app.store import ClinicalStore


class IngestRequest(BaseModel):
    records: list[str]


class RegisterCodeRequest(BaseModel):
    code: str
    display: str = ""
    unit: str = ""


class RegisterAliasRequest(BaseModel):
    alias: str
    target_code: str


class BatchReplayRequest(BaseModel):
    reason: str | None = None  # 可选：只重放某一失败类别


def _role(x_role: str | None) -> Role:
    try:
        return Role(x_role or "")
    except ValueError:
        raise HTTPException(status_code=401, detail="缺少或非法的角色标识")


def _reject_reason(value: str | None) -> RejectReason | None:
    if value is None or value == "":
        return None
    try:
        return RejectReason(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"非法失败类别: {value}；可选 malformed_format/missing_required_field/unknown_code",
        )


def _pending_status(value: str | None) -> PendingStatus | None:
    if value is None or value == "":
        return None
    try:
        return PendingStatus(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"非法状态: {value}；可选 pending/resolved")


def create_app(
    service: IngestService | None = None,
    *,
    salt: str = "dev-salt",
    quarantine_capacity: int | None = None,
    registry: CodeRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="clinical-data-interop")
    if service is None:
        quarantine = (
            QuarantineStore(capacity=quarantine_capacity)
            if quarantine_capacity is not None
            else QuarantineStore()
        )
        service = IngestService(
            ClinicalStore(), salt=salt, registry=registry, quarantine=quarantine
        )
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

    # -- 待处理区 -----------------------------------------------------------
    @app.get("/quarantine/records", response_model=None)
    def list_quarantine(
        x_role: str | None = Header(default=None),
        status: str | None = Query(default=None),
        reason: str | None = Query(default=None),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ):
        require(_role(x_role), "read_quarantine")
        result = service.quarantine.list(
            status=_pending_status(status),
            reason=_reject_reason(reason),
            start=start,
            end=end,
            page=page,
            page_size=page_size,
        )
        result["stats"] = service.quarantine.stats()
        return result

    @app.get("/quarantine/records/{pending_id}", response_model=None)
    def get_quarantine(pending_id: str, x_role: str | None = Header(default=None)):
        require(_role(x_role), "read_quarantine")
        record = service.quarantine.get(pending_id)
        if record is None:
            raise HTTPException(status_code=404, detail="待处理记录不存在或已被容量淘汰")
        return record.to_view()

    @app.post("/quarantine/records/{pending_id}/replay", response_model=None)
    def replay_one(pending_id: str, x_role: str | None = Header(default=None)):
        require(_role(x_role), "manage_quarantine")
        result = service.replay_one(pending_id)
        status_code = 404 if result.outcome.value == "not_found" else 200
        return JSONResponse(
            status_code=status_code,
            content={
                "pending_id": result.pending_id,
                "outcome": result.outcome.value,
                "reason": result.reason,
                "detail": result.detail,
                "ingest_outcome": result.ingest_outcome,
            },
        )

    @app.post("/quarantine/replay", response_model=None)
    def replay_batch(body: BatchReplayRequest, x_role: str | None = Header(default=None)):
        require(_role(x_role), "manage_quarantine")
        result = service.replay_pending(_reject_reason(body.reason))
        return {
            "triggered": result.triggered,
            "counts": result.counts(),
            "results": [
                {
                    "pending_id": r.pending_id,
                    "outcome": r.outcome.value,
                    "reason": r.reason,
                    "detail": r.detail,
                    "ingest_outcome": r.ingest_outcome,
                }
                for r in result.results
            ],
        }

    # -- 字典运行时维护 ------------------------------------------------------
    @app.post("/admin/codes", response_model=None)
    def register_code(body: RegisterCodeRequest, x_role: str | None = Header(default=None)):
        require(_role(x_role), "manage_quarantine")
        try:
            normalized = service.registry.register_code(body.code, body.display, body.unit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"code": normalized, "registered": True}

    @app.post("/admin/codes/{code}/aliases", response_model=None)
    def register_alias(code: str, body: RegisterAliasRequest,
                       x_role: str | None = Header(default=None)):
        require(_role(x_role), "manage_quarantine")
        # 路径参数 code 为权威目标，body.target_code 仅作交叉校验
        if body.target_code and body.target_code.strip().upper() != code.strip().upper():
            raise HTTPException(status_code=422, detail="路径编码与请求体 target_code 不一致")
        try:
            target = service.registry.register_alias(body.alias, code)
        except UnknownTargetCodeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"alias": body.alias.strip().upper(), "target_code": target}

    return app
