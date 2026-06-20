"""KSS REST API — pyramid DCA session management. Thin layer over app.kss.service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.kss import service
from app.security import require_api_key

router = APIRouter(prefix="/api/kss", tags=["KSS"])


# --- schemas ------------------------------------------------------------


class CreateSession(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    entry_price: float = Field(..., gt=0)
    distance_pct: float = Field(..., gt=0, lt=100)
    max_waves: int = Field(..., ge=1, le=100)
    isolated_fund: float = Field(..., gt=0)
    tp_pct: float = Field(..., gt=0)
    timeout_x_min: float = Field(30.0, gt=0)
    gap_y_min: float = Field(5.0, ge=0)
    note: str | None = None


class AdjustSession(BaseModel):
    max_waves: int | None = Field(None, ge=1, le=100)
    isolated_fund: float | None = Field(None, gt=0)
    tp_pct: float | None = Field(None, gt=0)
    distance_pct: float | None = Field(None, gt=0, lt=100)
    timeout_x_min: float | None = Field(None, gt=0)
    gap_y_min: float | None = Field(None, ge=0)


class PreviewRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    entry_price: float = Field(..., gt=0)
    distance_pct: float = Field(..., gt=0, lt=100)
    max_waves: int = Field(..., ge=1, le=100)
    isolated_fund: float = Field(..., gt=0)
    tp_pct: float = Field(..., gt=0)


# --- endpoints ----------------------------------------------------------


@router.post("/preview")
def preview(req: PreviewRequest):
    return service.preview(**req.model_dump())


@router.post("/sessions", dependencies=[Depends(require_api_key)])
def create_session(req: CreateSession, db: Session = Depends(get_db)):
    try:
        row = service.create_session(db, **req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.get_status(db, row.id)


@router.get("/sessions")
def list_sessions(
    status: str | None = Query(None),
    symbol: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    sessions = service.list_sessions(db, status=status, symbol=symbol, limit=limit)
    active = [s for s in sessions if s["status"] == "active"]
    return {
        "sessions": sessions,
        "total": len(sessions),
        "active_count": len(active),
        "total_isolated_fund": sum(s["isolated_fund"] for s in active),
    }


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    return service.summary(db)


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)):
    try:
        return service.get_status(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/start", dependencies=[Depends(require_api_key)])
def start_session(session_id: int, db: Session = Depends(get_db)):
    try:
        return service.start_session(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/stop", dependencies=[Depends(require_api_key)])
def stop_session(session_id: int, reason: str = "manual", db: Session = Depends(get_db)):
    try:
        return service.stop_session(db, session_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/sessions/{session_id}", dependencies=[Depends(require_api_key)])
def adjust_session(session_id: int, req: AdjustSession, db: Session = Depends(get_db)):
    try:
        return service.adjust_session(db, session_id, **req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/sessions/{session_id}", dependencies=[Depends(require_api_key)])
def delete_session(session_id: int, db: Session = Depends(get_db)):
    try:
        return service.delete_session(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/check-tp", dependencies=[Depends(require_api_key)])
def check_tp(session_id: int, current_price: float | None = None, db: Session = Depends(get_db)):
    try:
        return service.check_tp(db, session_id, current_price)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/take-profit", dependencies=[Depends(require_api_key)])
def take_profit(session_id: int, db: Session = Depends(get_db)):
    """Manual 'chốt lời ngay' — only valid while the session is in trailing mode (the UI gates it)."""
    try:
        return service.manual_take_profit(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class DcaNext(BaseModel):
    # Optional manual lever: deploy this many USD of idle cash this wave (None = geometric rung).
    amount_usd: float | None = Field(None, gt=0)


@router.post("/sessions/{session_id}/dca-next", dependencies=[Depends(require_api_key)])
def dca_next(session_id: int, body: DcaNext | None = None, db: Session = Depends(get_db)):
    """Manually queue the next DCA wave. ``amount_usd`` deploys a chosen USD slice of idle cash
    (incl. the auto-backup) and extends the ladder if full; omit it for the geometric rung."""
    try:
        return service.queue_next_wave(db, session_id, amount_usd=body.amount_usd if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
