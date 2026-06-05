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

from app import (
    charts,
    circuit,
    guardian,
    hyperopt,
    ml,
    notify,
    orders,
    portfolio,
    runtime,
    scanner,
    scheduler,
    timefmt,
)
from app.config import settings
from app.db import get_db
from app.kss import service as kss_service
from app.orchestrator import service as opus_service
from app.models import SESSION_ACTIVE, AgentVoteRecord, AuditLog, Candidate, KssSession, ScanRun
from app.security import require_api_key

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Display filters: money = ##,###.## (thousands + 2dp); qty keeps crypto precision.
templates.env.filters["money"] = lambda v: f"{float(v or 0):,.2f}"
templates.env.filters["qty"] = lambda v: f"{float(v or 0):,.6f}"
templates.env.filters["ladder"] = charts.pyramid_ladder_svg  # session dict -> SVG
# Display timezone: stored UTC -> local (Vietnam GMT+7) HH:MM:SS / full datetime.
templates.env.filters["hms"] = timefmt.local_hms
templates.env.filters["localdt"] = timefmt.local_dt

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


@api_router.post("/api/pending/approve-all", dependencies=[Depends(require_api_key)])
def approve_all(db: Session = Depends(get_db)):
    return {"approved": orders.approve_all(db)}


@api_router.post("/api/pending/reject-all", dependencies=[Depends(require_api_key)])
def reject_all(body: RejectBody, db: Session = Depends(get_db)):
    return {"rejected": orders.reject_all(db, reason=body.reason)}


@api_router.post("/api/pending/auto", dependencies=[Depends(require_api_key)])
def auto_process(db: Session = Depends(get_db)):
    """Run the auto-approval rule now (no-op unless autoapprove_enabled)."""
    return {"auto_approved": orders.auto_approve_by_policy(db)}


def _autoapprove_state() -> dict:
    return {
        "enabled": settings.autoapprove_enabled,
        "max_notional": settings.autoapprove_max_notional,
        "sources": settings.autoapprove_sources,
    }


@api_router.get("/api/autoapprove")
def autoapprove_state():
    return _autoapprove_state()


@api_router.post("/api/autoapprove", dependencies=[Depends(require_api_key)])
def set_autoapprove(body: AutoApproveBody, db: Session = Depends(get_db)):
    """Update the auto-approval rule. Persisted in runtime_config so it survives restarts."""
    runtime.set_autoapprove(db, enabled=body.enabled, max_notional=body.max_notional)
    return _autoapprove_state()


class AutoTradeBody(BaseModel):
    enabled: bool


class FullAutoBody(BaseModel):
    enabled: bool


class SchedulerBody(BaseModel):
    enabled: bool
    interval_min: int | None = Field(None, ge=1, le=1440)


class AutoApproveBody(BaseModel):
    enabled: bool
    max_notional: float | None = Field(None, gt=0)


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


@api_router.get("/api/performance")
def get_performance(db: Session = Depends(get_db)):
    return portfolio.performance_view(db)


def _automation_state(db: Session) -> dict:
    st = scheduler.status()
    active = db.query(KssSession).filter(KssSession.status == SESSION_ACTIVE).count()
    return {
        **st,
        "full_auto": settings.full_auto,
        "auto_trade": settings.auto_trade,
        "autoapprove": settings.autoapprove_enabled,
        "frozen": runtime.is_frozen(db),
        "open_sessions": active,
        "guardian": guardian.enabled(),
        "telegram": notify.is_running(),
        "hyperopt": settings.hyperopt_enabled,
        "ml": settings.ml_enabled,
    }


@api_router.get("/api/automation")
def get_automation(db: Session = Depends(get_db)):
    return _automation_state(db)


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


@api_router.get("/api/full-auto")
def get_full_auto(db: Session = Depends(get_db)):
    """Current full-auto master-switch + scheduler state."""
    return {**runtime.state(db), "scheduler_running": scheduler.is_running()}


@api_router.post("/api/full-auto", dependencies=[Depends(require_api_key)])
async def set_full_auto(body: FullAutoBody, db: Session = Depends(get_db)):
    """Enable or disable the full-auto master switch (persisted across restarts)."""
    if body.enabled:
        runtime.full_auto_on(db)
        scheduler.start()
    else:
        runtime.full_auto_off(db)
        scheduler.stop()
    return {**runtime.state(db), "scheduler_running": scheduler.is_running()}


class OpusBody(BaseModel):
    enabled: bool


@api_router.get("/api/opus")
def get_opus(db: Session = Depends(get_db)):
    """OPUS orchestrator mode state: switch, capital envelope, spend, KPI."""
    return opus_service.state(db)


@api_router.post("/api/opus", dependencies=[Depends(require_api_key)])
async def set_opus(body: OpusBody, db: Session = Depends(get_db)):
    """Enable/disable OPUS mode (persisted) and start/stop its independent decision loop.

    Must be async: the loop uses asyncio.create_task, which needs the running event loop.
    """
    from app.orchestrator import loop as opus_loop

    if body.enabled:
        runtime.opus_mode_on(db)
        opus_loop.start()
    else:
        runtime.opus_mode_off(db)
        opus_loop.stop()
    return {**opus_service.state(db), "loop_running": opus_loop.is_running()}


@api_router.post("/api/opus/shadow", dependencies=[Depends(require_api_key)])
def set_opus_shadow(body: OpusBody, db: Session = Depends(get_db)):
    """Toggle OPUS shadow mode (True = log intents only; False = execute on paper)."""
    runtime.opus_shadow_set(db, body.enabled)
    return opus_service.state(db)


@api_router.get("/api/opus/metrics")
def opus_metrics(hours: int = 48, db: Session = Depends(get_db)):
    """Hourly OPUS net-profit series + the current KPI/cost state (drives the chart)."""
    from app.orchestrator import ledger as opus_ledger

    rows = opus_ledger.metrics_series(db, hours=hours)
    return {
        **opus_service.state(db),
        "target_per_hour": opus_ledger.target_per_hour(),
        "series": [
            {
                "hour": r.hour_ts.isoformat(),
                "net_pnl": r.net_pnl,
                "gross_pnl": r.gross_pnl,
                "opus_cost_billed": r.opus_cost_billed,
                "net_pct": r.net_pct,
                "trades": r.trades,
                "win_trades": r.win_trades,
            }
            for r in rows
        ],
    }


@api_router.get("/api/breaker")
def get_breaker(db: Session = Depends(get_db)):
    """Circuit-breaker status with live metrics and configured thresholds."""
    return {
        **runtime.state(db),
        **circuit.metrics(db),
        "thresholds": {
            "max_drawdown_pct": settings.max_drawdown_pct,
            "daily_loss_hard_pct": settings.daily_loss_hard_pct,
            "max_consecutive_losses": settings.max_consecutive_losses,
        },
    }


@api_router.post("/api/breaker/reset", dependencies=[Depends(require_api_key)])
def reset_breaker(db: Session = Depends(get_db)):
    """Manually unfreeze the circuit-breaker (bypasses cooldown)."""
    return circuit.reset(db)


@api_router.get("/api/scheduler")
def scheduler_state():
    return {"enabled": scheduler.is_running(), "interval_min": settings.scan_interval_min}


@api_router.post("/api/scheduler", dependencies=[Depends(require_api_key)])
async def set_scheduler(body: SchedulerBody):
    """Start/stop the background scan+manage loop for this process (runs on the event loop)."""
    if body.interval_min:
        settings.scan_interval_min = body.interval_min
    if body.enabled:
        scheduler.start()
    else:
        scheduler.stop()
    return scheduler_state()


# --- Guardian endpoints -------------------------------------------------


class GuardianBody(BaseModel):
    enabled: bool


def _guardian_state() -> dict:
    return {
        "enabled": settings.guardian_enabled,
        "model": settings.guardian_model,
        "active": guardian.enabled(),
    }


@api_router.get("/api/guardian")
def get_guardian():
    return _guardian_state()


@api_router.post("/api/guardian", dependencies=[Depends(require_api_key)])
def set_guardian(body: GuardianBody):
    settings.guardian_enabled = body.enabled
    return _guardian_state()


# --- Telegram endpoints -------------------------------------------------


class TelegramBody(BaseModel):
    enabled: bool


def _telegram_state() -> dict:
    return {
        "enabled": settings.telegram_enabled,
        "running": notify.is_running(),
        "configured": notify.enabled(),
    }


@api_router.get("/api/telegram")
def get_telegram():
    return _telegram_state()


@api_router.post("/api/telegram", dependencies=[Depends(require_api_key)])
def set_telegram(body: TelegramBody):
    settings.telegram_enabled = body.enabled
    if body.enabled:
        notify.start()
    else:
        notify.stop()
    return _telegram_state()


@api_router.post("/api/telegram/test", dependencies=[Depends(require_api_key)])
def test_telegram():
    return {"sent": notify.send("FINDMY-FM test alert")}


# --- Phase C: hyperopt + ML endpoints -----------------------------------


class EnableBody(BaseModel):
    enabled: bool


@api_router.get("/api/params")
def get_params(db: Session = Depends(get_db)):
    from app.models import PairParams

    rows = db.query(PairParams).order_by(PairParams.score.desc()).all()
    return [r.to_dict() for r in rows]


@api_router.post("/api/hyperopt", dependencies=[Depends(require_api_key)])
def set_hyperopt(body: EnableBody):
    settings.hyperopt_enabled = body.enabled
    return {"enabled": settings.hyperopt_enabled}


@api_router.post("/api/hyperopt/run", dependencies=[Depends(require_api_key)])
def run_hyperopt(db: Session = Depends(get_db)):
    tuned = [hyperopt.run_for(db, s) for s in settings.watchlist]
    return {"tuned": [t.to_dict() for t in tuned if t is not None]}


@api_router.get("/api/ml")
def get_ml(db: Session = Depends(get_db)):
    m = ml.load_latest(db)
    return {"enabled": settings.ml_enabled, "model": m.to_dict() if m else None}


@api_router.post("/api/ml", dependencies=[Depends(require_api_key)])
def set_ml(body: EnableBody):
    settings.ml_enabled = body.enabled
    return {"enabled": settings.ml_enabled}


@api_router.post("/api/ml/retrain", dependencies=[Depends(require_api_key)])
def retrain_ml(db: Session = Depends(get_db)):
    m = ml.train(db)
    return {"trained": m.to_dict() if m else None}


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
    pend = orders.list_pending(db)
    prices = portfolio.get_current_prices(list({o.symbol for o in pend})) if pend else {}
    rows = []
    for o in pend:
        d = o.to_dict()
        ref = o.price if o.price > 0 else (prices.get(o.symbol) or 0.0)
        mkt = prices.get(o.symbol) or 0.0
        d["notional"] = o.quantity * ref
        # eligible to auto-clear by size+source; "due" = its limit price is reached now.
        d["auto"] = (o.source in settings.autoapprove_sources
                     and ref > 0 and d["notional"] <= settings.autoapprove_max_notional)
        d["due"] = (
            o.order_type == "MARKET"
            or (o.side == "BUY" and o.price > 0 and 0 < mkt <= o.price)
            or (o.side == "SELL" and o.price > 0 and mkt >= o.price)
        )
        rows.append(d)
    return templates.TemplateResponse(
        "partials/pending.html",
        {
            "request": request,
            "rows": rows,
            "aa_enabled": settings.autoapprove_enabled,
            "aa_max": settings.autoapprove_max_notional,
            "aa_sources": ",".join(settings.autoapprove_sources),
        },
    )


@ui_router.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/status.html", {"request": request, "a": _automation_state(db)}
    )


@ui_router.get("/partials/ladder", response_class=HTMLResponse)
def partial_ladder(
    request: Request,
    session: int | None = None,
    symbol: str | None = None,
    db: Session = Depends(get_db),
):
    """Big labelled price ladder for the click-to-view modal — by session id or by symbol
    (symbol resolves to that coin's active KSS session)."""
    sid = session
    if sid is None and symbol:
        active = kss_service.list_sessions(db, status=SESSION_ACTIVE, symbol=symbol)
        sid = active[0]["id"] if active else None
    if sid is None:
        return templates.TemplateResponse(
            "partials/ladder.html",
            {"request": request, "o": None,
             "msg": f"{symbol or ''} chưa có session KSS đang chạy."},
        )
    try:
        st = kss_service.ladder_status(db, sid)
    except ValueError:
        return templates.TemplateResponse(
            "partials/ladder.html", {"request": request, "o": None, "msg": "Session không tồn tại."}
        )
    return templates.TemplateResponse(
        "partials/ladder.html",
        {"request": request, "o": st, "ladder_svg": charts.price_ladder_svg(st)},
    )


@ui_router.get("/partials/opus", response_class=HTMLResponse)
def partial_opus(request: Request, db: Session = Depends(get_db)):
    from app.orchestrator import ledger as opus_ledger

    # Read-only: the OPUS loop tick owns rollup writes (avoids SQLite write contention
    # with the scheduler when this partial is polled).
    rows = opus_ledger.metrics_series(db, hours=48)
    labels = [r.hour_ts.isoformat() for r in rows]
    nets = [r.net_pnl for r in rows]
    return templates.TemplateResponse(
        "partials/opus.html",
        {
            "request": request,
            "o": opus_service.state(db),
            "pnl_svg": charts.opus_hourly_pnl_svg(labels, nets),
            "cum_svg": charts.opus_cumulative_vs_target_svg(nets, opus_ledger.target_per_hour()),
        },
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
            "sched": scheduler.is_running(),
            "interval": settings.scan_interval_min,
            "min_win_rate": settings.min_win_rate,
            "min_confidence": settings.min_confidence,
            "deadline_days": settings.deadline_days,
        },
    )


@ui_router.get("/partials/performance", response_class=HTMLResponse)
def partial_performance(request: Request, db: Session = Depends(get_db)):
    p = portfolio.performance_view(db)
    return templates.TemplateResponse(
        "partials/performance.html",
        {
            "request": request,
            "p": p,
            "equity_svg": charts.equity_curve_svg(p["equity_curve"], p["equity_times"]),
            "winloss_svg": charts.winloss_bars_svg(p["wins"], p["losses"]),
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
