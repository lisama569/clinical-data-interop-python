"""FastAPI 路由：批次摄取、明细查询、导出、待处理区查阅与重放。

所有读取路径只返回脱敏数据；待处理区原始报文绝不随任何响应输出。
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import Role, require
from app.codes import CodeRegistryConflictError, register_alias, register_code
from app.errors import PermissionDeniedError
from app.models import BatchResult, RejectReason
from app.quarantine import QuarantineStatus, ReplayOutcome
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
    code: str


class ReplayBatchRequest(BaseModel):
    reason: str | None = None
    code_token: str | None = None


def _role(x_role: str | None) -> Role:
    try:
        return Role(x_role or "")
    except ValueError:
        raise HTTPException(status_code=401, detail="缺少或非法的角色标识")


def _entry_dict(entry) -> dict:
    """序列化待处理条目：剥离原始报文，枚举取值，保证响应无敏感原文。"""
    data = asdict(entry)  # 嵌套 dataclass（history 事件）会一并转为 dict
    data.pop("_payload_raw", None)
    data["reason"] = entry.reason.value
    data["status"] = entry.status.value
    return data


def _reject_reason(value: str | None) -> RejectReason | None:
    if value is None or value == "":
        return None
    try:
        return RejectReason(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法失败类别: {value}")


def _qstatus(value: str | None) -> QuarantineStatus | None:
    if value is None or value == "":
        return None
    try:
        return QuarantineStatus(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法状态: {value}")


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

    # ---- 待处理区 ----

    @app.get("/quarantine", response_model=None)
    def quarantine_list(
        x_role: str | None = Header(default=None),
        reason: str | None = Query(default=None),
        status: str | None = Query(default=None),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        # 无权限直接 403：明确拒绝，而不是返回空列表
        require(_role(x_role), "read_quarantine")
        page = service.quarantine.list_page(
            reason=_reject_reason(reason), status=_qstatus(status),
            start=start, end=end, limit=limit, offset=offset,
        )
        return {
            "items": [_entry_dict(e) for e in page.items],
            "total": page.total, "limit": page.limit, "offset": page.offset,
            "counts": service.quarantine.counts(),
        }

    @app.get("/quarantine/{entry_id}", response_model=None)
    def quarantine_detail(entry_id: str, x_role: str | None = Header(default=None)):
        require(_role(x_role), "read_quarantine")
        entry = service.quarantine.get(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="待处理记录不存在")
        if entry.status is QuarantineStatus.EVICTED:
            raise HTTPException(status_code=410, detail="该待处理记录已按容量策略淘汰")
        return _entry_dict(entry)

    @app.post("/quarantine/replay/{entry_id}", response_model=None)
    def quarantine_replay_one(entry_id: str, x_role: str | None = Header(default=None)):
        require(_role(x_role), "replay_quarantine")
        result = service.replay_pending(entry_id)
        if result.outcome is ReplayOutcome.NOT_FOUND:
            raise HTTPException(status_code=404, detail="待处理记录不存在或已淘汰")
        return {
            "entry_id": result.entry_id,
            "outcome": result.outcome.value,
            "record_outcome": result.record_outcome,
            "reason": result.reason,
            "detail": result.detail,
        }

    @app.post("/quarantine/replay", response_model=None)
    def quarantine_replay_batch(
        body: ReplayBatchRequest | None = None,
        x_role: str | None = Header(default=None),
    ):
        require(_role(x_role), "replay_quarantine")
        reason = _reject_reason(body.reason if body else None)
        summary = service.replay_batch(
            reason=reason, code_token=(body.code_token if body else None),
        )
        return {
            "results": [
                {
                    "entry_id": r.entry_id,
                    "outcome": r.outcome.value,
                    "record_outcome": r.record_outcome,
                    "reason": r.reason,
                    "detail": r.detail,
                }
                for r in summary["results"]
            ],
            "counts": summary["counts"],
        }

    @app.post("/admin/codes", response_model=None)
    def register_code_or_alias(
        body: dict,
        x_role: str | None = Header(default=None),
    ):
        """运行时扩展字典（不重启）。

        - {"code": "9999-9", "display": ..., "unit": ...}：新增标准编码；
        - {"alias": "CRP", "code": "1988-5"}：为已注册编码新增别名。
        """
        require(_role(x_role), "replay_quarantine")
        try:
            if "alias" in body:
                payload = RegisterAliasRequest(**body)
                target = register_alias(payload.alias, payload.code)
                return {"registered": "alias", "alias": payload.alias.strip().upper(),
                        "code": target}
            payload = RegisterCodeRequest(**body)
            code = register_code(payload.code, payload.display, payload.unit)
            return {"registered": "code", "code": code}
        except (CodeRegistryConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    return app
