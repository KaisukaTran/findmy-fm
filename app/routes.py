"""
Core HTTP layer: JSON API + server-rendered dashboard (HTMX) + WebSocket.

Routes are thin — they validate input, call a domain/read function, and return
either JSON or an HTML fragment. No business logic lives here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import orders, portfolio, scanner
from app.config import settings
from app.db import get_db
from app.kss import service as kss_service
from app.models import AgentVoteRecord, AuditLog, Candidate, ScanRun
from app.security import require_api_key

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

api_router = APIRouter()
ui_router = APIRouter()


# --- request models -----------------------------------------------------


class ManualOrder(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    side: str = Field(..., pattern="^(BUY|SELL|buy|sell)$")
    quantity: float | None = Field(None, gt=0)
    pips: float | None = Field(None, gt=0)
    price: float = Field(0.0, ge=0)
    order_type: str = Field("LIMIT", max_length=12)


class RejectBody(BaseModel):
    reason: str = Field("", max_length=500)


# --- JSON API -----------------------------------------------------------


@api_router.get("/health")
def health():
    return {"status": "ok"}


@api_router.get("/api/summary")
def get_summary(db: Session = Depends(get_db)):
    return portfolio.summary_view(db)


@api_router.get("/api/positions")
def get_positions(db: Session = Depends(get_db)):
    return portfolio.positions_view(db)


@api_router.get("/api/trades")
def get_trades(limit: int = 50, db: Session = Depends(get_db)):
    return portfolio.trades_view(db, limit=limit)


@api_router.get("/api/pending")
def get_pending(status: str | None = None, db: Session = Depends(get_db)):
    return [o.to_dict() for o in orders.list_pending(db, status=status)]


@api_router.post("/api/orders", dependencies=[Depends(require_api_key)])
def create_order(body: ManualOrder, db: Session = Depends(get_db)):
    try:
        order, risk_note = orders.queue_order(
            db,
            symbol=body.symbol,
            side=body.side,
            quantity=body.quantity,
            pips=body.pips,
            price=body.price,
            order_type=body.order_type,
            source="manual",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"order": order.to_dict(), "risk_note": risk_note}


@api_router.post("/api/pending/approve/{order_id}", dependencies=[Depends(require_api_key)])
def approve(order_id: int, db: Session = Depends(get_db)):
    try:
        fill = orders.approve_order(db, order_id, reviewer="dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "approved", "fill": fill.to_dict()}


@api_router.post("/api/pending/reject/{order_id}", dependencies=[Depends(require_api_key)])
def reject(order_id: int, body: RejectBody, db: Session = Depends(get_db)):
    try:
        order = orders.reject_order(db, order_id, reason=body.reason, reviewer="dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "rejected", "order": order.to_dict()}


class AutoTradeBody(BaseModel):
    enabled: bool


def _latest_candidates(db: Session) -> list[dict]:
    scan = db.query(ScanRun).order_by(ScanRun.id.desc()).first()
    if not scan:
        return []
    rows = (
        db.query(Candidate)
        .filter(Candidate.scan_id == scan.id)
        .order_by(Candidate.consensus_pct.desc())
        .all()
    )
    return [c.to_dict() for c in rows]


@api_router.post("/api/scan", dependencies=[Depends(require_api_key)])
def run_scan(db: Session = Depends(get_db)):
    """Run one multi-agent scan; creates sessions per the current auto-trade mode."""
    return scanner.run_scan(db)


@api_router.get("/api/candidates")
def get_candidates(db: Session = Depends(get_db)):
    return _latest_candidates(db)


@api_router.get("/api/agents/decisions")
def get_agent_decisions(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(AgentVoteRecord).order_by(AgentVoteRecord.id.desc()).limit(limit).all()
    return [r.to_dict() for r in rows]


@api_router.get("/api/audit")
def get_audit(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return [r.to_dict() for r in rows]


@api_router.get("/api/autotrade")
def autotrade_state():
    return {
        "auto_trade": settings.auto_trade,
        "min_win_rate": settings.min_win_rate,
        "min_confidence": settings.min_confidence,
        "deadline_days": settings.deadline_days,
    }


@api_router.post("/api/autotrade", dependencies=[Depends(require_api_key)])
def set_autotrade(body: AutoTradeBody):
    """Toggle full-auto for this process. Persist via env/.env for restarts."""
    settings.auto_trade = body.enabled
    return autotrade_state()


# --- dashboard (HTMX) ---------------------------------------------------


@ui_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@ui_router.get("/partials/summary", response_class=HTMLResponse)
def partial_summary(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/summary.html", {"request": request, "s": portfolio.summary_view(db)}
    )


@ui_router.get("/partials/positions", response_class=HTMLResponse)
def partial_positions(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/positions.html", {"request": request, "rows": portfolio.positions_view(db)}
    )


@ui_router.get("/partials/trades", response_class=HTMLResponse)
def partial_trades(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/trades.html", {"request": request, "rows": portfolio.trades_view(db)}
    )


@ui_router.get("/partials/pending", response_class=HTMLResponse)
def partial_pending(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/pending.html",
        {"request": request, "rows": [o.to_dict() for o in orders.list_pending(db)]},
    )


@ui_router.get("/partials/kss", response_class=HTMLResponse)
def partial_kss(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/kss.html",
        {
            "request": request,
            "sessions": kss_service.list_sessions(db),
            "summary": kss_service.summary(db),
        },
    )


@ui_router.get("/partials/scanner", response_class=HTMLResponse)
def partial_scanner(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/scanner.html",
        {
            "request": request,
            "rows": _latest_candidates(db),
            "auto": settings.auto_trade,
            "min_win_rate": settings.min_win_rate,
            "min_confidence": settings.min_confidence,
            "deadline_days": settings.deadline_days,
        },
    )


@ui_router.get("/partials/audit", response_class=HTMLResponse)
def partial_audit(request: Request, db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(40).all()
    return templates.TemplateResponse(
        "partials/audit.html", {"request": request, "rows": [r.to_dict() for r in rows]}
    )


# --- WebSocket push -----------------------------------------------------


@ui_router.websocket("/ws")
async def ws(websocket: WebSocket):
    """Emit a periodic 'refresh' tick so the client re-fetches partials."""
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"event": "refresh"})
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        return
