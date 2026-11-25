"""车队批次路由：批次建档、逐车确认、原子组合确认、换车重算与车队视图。

所有写接口沿用统一幂等框架：重复提交原样回放首次响应，不会多占容量。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import IdempotentConflict, idempotent_endpoint
from ..models import FleetBatch
from ..schemas import (
    FleetBatchCreate,
    FleetBatchDetailOut,
    FleetBatchOut,
    FleetConfirmOut,
    FleetItemConfirmRequest,
    FleetReplanRequest,
)
from ..services import fleet as service

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


def _serialize(db: Session, batch: FleetBatch) -> dict:
    view = service.batch_view(db, batch)
    payload = FleetBatchOut.model_validate(batch).model_dump(mode="json")
    payload["deficit"] = view["deficit"]
    payload["unmet_items"] = view["unmet_items"]
    return payload


@router.post("/batches", response_model=FleetBatchDetailOut)
def create_batch(req: FleetBatchCreate, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work() -> dict:
        batch = service.create_batch(db, req)
        return _serialize(db, batch)

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)


@router.get("/batches", response_model=list[FleetBatchDetailOut])
def list_batches(db: Session = Depends(get_db)):
    batches = db.scalars(select(FleetBatch).order_by(FleetBatch.id.desc())).all()
    return [_serialize(db, b) for b in batches]


@router.get("/batches/{batch_id}", response_model=FleetBatchDetailOut)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    from fastapi import HTTPException
    batch = db.get(FleetBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "车队批次不存在")
    return _serialize(db, batch)


@router.post("/batches/{batch_id}/items/{item_id}/confirm", response_model=FleetConfirmOut)
def confirm_item(batch_id: int, item_id: int, req: FleetItemConfirmRequest,
                 request: Request, db: Session = Depends(get_db)):
    payload = {"batch_id": batch_id, "item_id": item_id, **req.model_dump(mode="json")}

    def work() -> dict:
        batch, outcome, message = service.confirm_item(
            db, batch_id, item_id, req.preferred_connector_id
        )
        body = _serialize(db, batch)
        body.update({
            "outcome": outcome,
            "outcome_item_id": item_id,
            "message": message,
        })
        return body

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)


@router.post("/batches/{batch_id}/atomic-confirm", response_model=FleetConfirmOut)
def atomic_confirm(batch_id: int, req: FleetItemConfirmRequest,
                   request: Request, db: Session = Depends(get_db)):
    payload = {"batch_id": batch_id, **req.model_dump(mode="json")}

    def work() -> dict:
        batch, outcome, message = service.solve_atomic(db, batch_id)
        body = _serialize(db, batch)
        body.update({
            "outcome": outcome,
            "outcome_item_id": 0,
            "message": message,
        })
        return body

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)


@router.post("/batches/{batch_id}/items/{item_id}/replan", response_model=FleetBatchDetailOut)
def replan_item(batch_id: int, item_id: int, req: FleetReplanRequest,
                request: Request, db: Session = Depends(get_db)):
    payload = {
        "batch_id": batch_id, "item_id": item_id, **req.model_dump(mode="json"),
    }

    def work() -> dict:
        batch = service.replan_item(db, batch_id, item_id, req)
        return _serialize(db, batch)

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)
