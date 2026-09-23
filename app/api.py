"""HTTP 接口。

- 角色通过 ``X-Role`` 请求头注入（演示用 RBAC 边界；生产应替换为鉴权中间件）；
- 越权统一 403，错误体只含动作名，绝不回显业务数据；
- 冲突语义：VERSION_CONFLICT/DIVERGENT_CONTENT -> 409，IDEMPOTENT_REPLAY -> 200 标记。
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.errors import (
    AccessDenied,
    BatchInconsistent,
    ClinicalInteropError,
    ConflictError,
)
from app.models import Format, Role
from app.security import role_from_header
from app.service import IngestionService

logger = logging.getLogger("clinical.interop.api")

router = APIRouter()

# 演示用单一服务实例（进程内仓库）
service = IngestionService()


def _role(x_role: str | None) -> Role:
    return role_from_header(x_role)


def _rejection_dto(r) -> dict:
    data = asdict(r)
    if hasattr(r.reason, "value"):
        data["reason"] = r.reason.value
    if r.resource_type is not None and hasattr(r.resource_type, "value"):
        data["resource_type"] = r.resource_type.value
    return data


@router.post("/ingest/{batch_id}")
async def ingest(
    batch_id: str,
    request: Request,
    fmt: str | None = Query(default=None, alias="f"),
    x_role: str | None = Header(default=None),
):
    """提交一个导入批次（body 为原始 FHIR JSON 或 CSV 文本）。"""

    # 先鉴权，再校验格式参数，避免匿名请求得到参数错误而非 403
    try:
        role = _role(x_role)
        from app.security import require as _require

        _require(role, "ingest:submit")
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not fmt:
        raise HTTPException(status_code=400, detail="query parameter 'f' (format) is required")
    try:
        fmt_enum = Format(fmt)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unsupported format: {fmt}")
    payload = (await request.body()).decode("utf-8")
    try:
        result = service.ingest_batch(
            batch_id=batch_id, fmt=fmt_enum, payload=payload, role=role
        )
    except BatchInconsistent as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "batch_id": result.batch_id,
        "status": result.status.value,
        "replay": result.replay,
        "accepted_counts": result.accepted_counts,
        "rejected_count": result.rejected_count,
        "conflicts": list(result.conflicts),
        "rejections": [_rejection_dto(r) for r in result.rejections],
    }


@router.get("/patients/{source_system}/{source_id}")
async def patient_detail(
    source_system: str,
    source_id: str,
    x_role: str | None = Header(default=None),
):
    try:
        view = service.get_patient_view(
            source_system=source_system, source_id=source_id, role=_role(x_role)
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not view:
        raise HTTPException(status_code=404, detail="patient not found")
    return view


@router.get("/patients/{patient_source_id}/observations")
async def patient_observations(
    patient_source_id: str,
    x_role: str | None = Header(default=None),
):
    try:
        return service.list_patient_observations(
            patient_source_id=patient_source_id, role=_role(x_role)
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/rejections")
async def list_rejections(
    batch_id: str | None = Query(default=None),
    x_role: str | None = Header(default=None),
):
    try:
        return service.list_rejections(batch_id=batch_id, role=_role(x_role))
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/rejections/{batch_id}/{index}/fragment")
async def rejected_fragment(
    batch_id: str,
    index: int,
    x_role: str | None = Header(default=None),
):
    try:
        view = service.get_rejected_fragment(batch_id=batch_id, index=index, role=_role(x_role))
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not view:
        raise HTTPException(status_code=404, detail="rejected fragment not found")
    return view


@router.get("/export/observations")
async def export_observations(x_role: str | None = Header(default=None)):
    try:
        return service.export_observations(role=_role(x_role))
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.put("/observations/{source_system}/{source_id}")
async def update_observation(
    source_system: str,
    source_id: str,
    request: Request,
    x_role: str | None = Header(default=None),
):
    import json

    from app.errors import RecordRejected

    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="request body must be JSON")
    try:
        dto, verdict = service.update_observation(
            source_system=source_system,
            source_id=source_id,
            value=float(body["value"]),
            unit=str(body["unit"]),
            expected_version=int(body["expected_version"]),
            role=_role(x_role),
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"verdict": exc.verdict.value, "resource": f"{exc.resource_type}/{exc.source_id}"},
        )
    except RecordRejected as exc:
        # 更新值单位不兼容 / 编码问题（detail 仅含编码与单位，不含标识）
        raise HTTPException(status_code=422, detail={"reason": exc.reason.value, "basis": exc.detail})
    except ClinicalInteropError:
        raise HTTPException(status_code=404, detail="observation not found")
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="value/unit/expected_version required and must be valid")
    return {"verdict": verdict.value, "observation": dto}
