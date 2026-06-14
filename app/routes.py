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
    costs,
    execution,
    guardian,
    hyperopt,
    ml,
    notify,
    notify_discord,
    orders,
    pnlcal,
    portfolio,
    runtime,
    savings,
    scanner,
    scheduler,
    timefmt,
)
from app.config import settings
from app.db import get_db
from app.kss import service as kss_service
from app.models import SESSION_ACTIVE, AgentVoteRecord, AuditLog, Candidate, KssSession, ScanRun
from app.orchestrator import service as opus_service
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
        "discord": notify_discord.is_running() or notify_discord.webhook_enabled(),
        "hyperopt": settings.hyperopt_enabled,
        "ml": settings.ml_enabled,
        "grok_scanner": settings.grok_scanner_enabled,
        "ta_lib": settings.ta_lib_enabled,
        "ta_external": settings.ta_external_enabled,
        "live_trading": settings.live_trading,
        "live_keys": execution.live_key_present(),
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


class LiveTradingBody(BaseModel):
    enabled: bool
    confirm: str | None = None


_LIVE_CONFIRM_PHRASE = "LIVE-TRADING"


@api_router.get("/api/live-trading")
def get_live_trading(db: Session = Depends(get_db)):
    """Current go-live state: master flag, whether exchange keys are present, breaker."""
    return {
        "live_trading": settings.live_trading,
        "live_keys": execution.live_key_present(),
        "exchange": settings.live_exchange,
        "max_notional": settings.live_max_order_notional,
        "frozen": runtime.is_frozen(db),
        "confirm_phrase": _LIVE_CONFIRM_PHRASE,
    }


@api_router.post("/api/live-trading", dependencies=[Depends(require_api_key)])
def set_live_trading(body: LiveTradingBody, db: Session = Depends(get_db)):
    """Flip the real-money master switch.

    Enabling requires a typed confirmation phrase AND configured exchange keys AND an
    armed circuit breaker (a tripped breaker blocks going live). Disabling is always
    allowed and immediate.
    """
    if body.enabled:
        if (body.confirm or "").strip() != _LIVE_CONFIRM_PHRASE:
            raise HTTPException(
                status_code=400,
                detail=f"Type '{_LIVE_CONFIRM_PHRASE}' to confirm real-money trading",
            )
        if not execution.live_key_present():
            raise HTTPException(
                status_code=400, detail="Exchange API key/secret not configured — cannot go live"
            )
        if runtime.is_frozen(db):
            raise HTTPException(
                status_code=409, detail="Circuit-breaker frozen — resolve it before going live"
            )
    runtime.set_live_trading(db, body.enabled)
    return get_live_trading(db)


class CloseBody(BaseModel):
    symbol: str


@api_router.post("/api/positions/close", dependencies=[Depends(require_api_key)])
def close_position(body: CloseBody, db: Session = Depends(get_db)):
    """User override: stop any active KSS session for the coin, then market-sell the whole
    held position (last-resort manual exit)."""
    from app.models import SESSION_ACTIVE, KssSession, Position

    for s in db.query(KssSession).filter(
        KssSession.symbol == body.symbol, KssSession.status == SESSION_ACTIVE
    ).all():
        try:
            kss_service.stop_session(db, s.id, reason="user close-all")
        except ValueError:
            pass
    p = db.query(Position).filter(Position.symbol == body.symbol).one_or_none()
    if p is None or p.quantity <= 0:
        return {"closed": False, "reason": "no position"}
    order, _ = orders.queue_order(db, symbol=body.symbol, side="SELL", quantity=p.quantity,
                                  price=0.0, order_type="MARKET", source="manual",
                                  source_ref="manual:close", note="user close-all")
    fill = orders.approve_order(db, order.id, reviewer="dashboard")
    return {"closed": True, "realized": fill.realized_pnl, "qty": fill.quantity}


class KssSettingsBody(BaseModel):
    scan_distance_pct: float | None = Field(None, gt=0, le=50)
    scan_tp_pct: float | None = Field(None, gt=0, le=100)
    scan_max_waves: int | None = Field(None, ge=1, le=50)
    scan_fund: float | None = Field(None, gt=0)
    sl_pct: float | None = Field(None, ge=0, le=100)
    trailing_pct: float | None = Field(None, ge=0, le=100)
    deadline_days: int | None = Field(None, ge=1, le=365)
    max_concurrent_sessions: int | None = Field(None, ge=1, le=100)
    max_deployed_pct: float | None = Field(None, gt=0, le=100)
    loss_streak_block_k: int | None = Field(None, ge=1, le=20)
    loss_streak_window_days: int | None = Field(None, ge=1, le=365)
    min_expectancy_pct: float | None = Field(None, ge=-100, le=100)
    min_win_rate: float | None = Field(None, ge=0, le=100)
    min_confidence: float | None = Field(None, ge=0, le=100)  # S4: consensus threshold
    grok_scanner_fail_mode: str | None = Field(None, pattern=r"^(open|closed)$")  # S5
    scan_max_symbols: int | None = Field(None, ge=1, le=500)
    min_quote_volume: float | None = Field(None, ge=0)
    kss_first_wave_usd: float | None = Field(None, ge=0)


@api_router.get("/api/kss-settings")
def get_kss_settings(db: Session = Depends(get_db)):
    return runtime.kss_settings(db)


@api_router.post("/api/kss-settings", dependencies=[Depends(require_api_key)])
def set_kss_settings(body: KssSettingsBody, db: Session = Depends(get_db)):
    """Update the master KSS knobs (applied to NEW sessions). Persisted across restarts."""
    return runtime.set_kss_settings(db, body.model_dump(exclude_none=True))


class ConsensusWeightsBody(BaseModel):
    """S4: runtime-editable consensus agent weights. backtest is always 0."""
    trend: float | None = Field(None, ge=0, le=1)
    dip: float | None = Field(None, ge=0, le=1)
    volatility: float | None = Field(None, ge=0, le=1)
    liquidity: float | None = Field(None, ge=0, le=1)
    ml: float | None = Field(None, ge=0, le=1)


@api_router.get("/api/consensus-weights")
def get_consensus_weights(db: Session = Depends(get_db)):
    """Return the active consensus agent weights (backtest always 0)."""
    return runtime.get_consensus_weights(db)


@api_router.post("/api/consensus-weights", dependencies=[Depends(require_api_key)])
def set_consensus_weights_route(body: ConsensusWeightsBody, db: Session = Depends(get_db)):
    """Update consensus agent weights. Persisted across restarts. backtest forced to 0."""
    return runtime.set_consensus_weights(db, body.model_dump(exclude_none=True))


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


@api_router.post("/api/grok", dependencies=[Depends(require_api_key)])
def set_grok(body: OpusBody, db: Session = Depends(get_db)):
    """Toggle the Grok co-pilot (consensus with OPUS). Needs xai_api_key to be active."""
    runtime.grok_set(db, body.enabled)
    return opus_service.state(db)


@api_router.post("/api/grok-scanner", dependencies=[Depends(require_api_key)])
def set_grok_scanner(body: OpusBody, db: Session = Depends(get_db)):
    """Toggle the Grok scanner gate (endorse/veto over qualified candidates). Independent of
    OPUS mode; needs xai_api_key to actually run."""
    runtime.grok_scanner_set(db, body.enabled)
    return runtime.state(db)


class TaSourceBody(BaseModel):
    source: str  # "lib" (Tier 2 pandas-ta) | "external" (Tier 3 taapi.io)
    enabled: bool


@api_router.post("/api/ta-source", dependencies=[Depends(require_api_key)])
def set_ta_source(body: TaSourceBody, db: Session = Depends(get_db)):
    """Toggle an optional TA overlay that enriches the Grok gate. Tier 1 indicators are always
    on, so this only adds/removes the pandas-ta (lib) or external (taapi.io) overlay."""
    if body.source == "lib":
        runtime.ta_source_set(db, lib=body.enabled)
    elif body.source == "external":
        runtime.ta_source_set(db, external=body.enabled)
    return runtime.state(db)


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


# --- Discord endpoints --------------------------------------------------


class DiscordBody(BaseModel):
    enabled: bool


def _discord_state() -> dict:
    return {
        "enabled": settings.discord_enabled,
        "running": notify_discord.is_running(),  # command gateway alive
        "webhook": notify_discord.webhook_enabled(),  # push configured
        "commands": notify_discord.command_enabled(),  # 2-way configured
    }


@api_router.get("/api/discord")
def get_discord():
    return _discord_state()


@api_router.post("/api/discord", dependencies=[Depends(require_api_key)])
def set_discord(body: DiscordBody):
    settings.discord_enabled = body.enabled
    if body.enabled:
        notify_discord.start()  # no-op unless a bot token + channel id are set
    else:
        notify_discord.stop()
    return _discord_state()


@api_router.post("/api/discord/test", dependencies=[Depends(require_api_key)])
def test_discord():
    return {"sent": notify_discord.send("FINDMY-FM test alert")}


# --- Cost endpoints (trade fee + withdrawal + VAT + AI) -----------------


class WithdrawalBody(BaseModel):
    amount: float = Field(..., gt=0, description="Withdrawn amount in USD.")
    note: str | None = Field(None, max_length=200)


@api_router.post("/api/withdrawals", dependencies=[Depends(require_api_key)])
def create_withdrawal(body: WithdrawalBody, db: Session = Depends(get_db)):
    try:
        w = costs.record_withdrawal(db, body.amount, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"withdrawal": w.to_dict()}


@api_router.get("/api/withdrawals")
def list_withdrawals(limit: int = 50, db: Session = Depends(get_db)):
    return {"rows": [w.to_dict() for w in costs.list_withdrawals(db, limit=limit)]}


@api_router.get("/api/costs")
def get_costs(period: str = "month", buckets: int = 12, db: Session = Depends(get_db)):
    return costs.cost_summary(db, period=period, buckets=buckets)


@ui_router.get("/partials/costs", response_class=HTMLResponse)
def partial_costs(request: Request, period: str = "month", db: Session = Depends(get_db)):
    buckets = {"week": 12, "month": 12, "year": 5}.get(period, 12)
    summary = costs.cost_summary(db, period=period, buckets=buckets)
    recent = [w.to_dict() for w in costs.list_withdrawals(db, limit=10)]
    return templates.TemplateResponse(
        "partials/costs.html",
        {
            "request": request,
            "s": summary,
            "period": period,
            "withdrawals": recent,
            "fee_pct": settings.withdrawal_fee_pct + settings.withdrawal_fee_tolerance_pct,
            "vat_pct": settings.vat_pct,
            "ai_claude_est": settings.ai_monthly_claude_usd,
            "ai_grok_est": settings.ai_monthly_grok_usd,
        },
    )


# --- Savings (KAI) holdings — protected, never auto-sold ----------------


class SavingsBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    quantity: float = Field(..., gt=0)
    avg_cost: float = Field(..., ge=0, description="USD price/unit of this buy.")
    note: str | None = Field(None, max_length=200)
    mode: str = Field("add", pattern="^(add|set)$", description="add = accumulate; set = overwrite.")


@api_router.post("/api/savings", dependencies=[Depends(require_api_key)])
def create_savings(body: SavingsBody, db: Session = Depends(get_db)):
    try:
        fn = savings.set_holding if body.mode == "set" else savings.add_holding
        h = fn(db, body.symbol, body.quantity, body.avg_cost, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"holding": h.to_dict()}


@api_router.delete("/api/savings/{symbol}", dependencies=[Depends(require_api_key)])
def delete_savings(symbol: str, db: Session = Depends(get_db)):
    return {"removed": savings.remove_holding(db, symbol)}


@api_router.get("/api/savings")
def get_savings(db: Session = Depends(get_db)):
    return savings.summary(db)


@ui_router.get("/partials/savings", response_class=HTMLResponse)
def partial_savings(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/savings.html", {"request": request, "s": savings.summary(db)}
    )


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
def partial_positions(request: Request, page: int = 1, db: Session = Depends(get_db)):
    # Trading tab: page size 20, up to 10 pages.
    page = max(1, min(page, 10))
    offset = (page - 1) * 20
    all_rows = portfolio.positions_view(db)
    rows = all_rows[offset: offset + 20]
    return templates.TemplateResponse(
        "partials/positions.html",
        {
            "request": request,
            "rows": rows,
            "page": page,
            "has_prev": page > 1,
            "has_next": len(all_rows) > offset + 20,
        },
    )


@ui_router.get("/partials/trades", response_class=HTMLResponse)
def partial_trades(request: Request, page: int = 1, db: Session = Depends(get_db)):
    page = max(1, min(page, 10))
    offset = (page - 1) * 20
    rows = portfolio.trades_view(db, limit=20, offset=offset)
    return templates.TemplateResponse(
        "partials/trades.html",
        {
            "request": request,
            "rows": rows,
            "page": page,
            "has_prev": page > 1,
            "has_next": len(rows) == 20,
        },
    )


@ui_router.get("/partials/pending", response_class=HTMLResponse)
def partial_pending(request: Request, page: int = 1, db: Session = Depends(get_db)):
    page = max(1, min(page, 10))
    offset = (page - 1) * 20
    pend = orders.list_pending(db, limit=20, offset=offset)
    prices = portfolio.get_current_prices(list({o.symbol for o in pend})) if pend else {}
    rows = []
    for o in pend:
        d = o.to_dict()
        ref = o.price if o.price > 0 else (prices.get(o.symbol) or 0.0)
        mkt = prices.get(o.symbol) or 0.0
        d["mkt"] = mkt  # current market price (for the "Giá hiện tại" column)
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
            "page": page,
            "has_prev": page > 1,
            "has_next": len(rows) == 20,
        },
    )


@ui_router.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/status.html", {"request": request, "a": _automation_state(db)}
    )


@api_router.get("/api/losses")
def api_losses(db: Session = Depends(get_db)):
    """Loss analysis (every losing fill + breakdowns) as JSON."""
    return portfolio.loss_analysis(db)


@ui_router.get("/partials/losses", response_class=HTMLResponse)
def partial_losses(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "partials/losses.html", {"request": request, "L": portfolio.loss_analysis(db)}
    )


@ui_router.get("/partials/live-trading", response_class=HTMLResponse)
def partial_live_trading(request: Request, db: Session = Depends(get_db)):
    """Go-live control: current paper/live posture + the typed-confirm toggle."""
    return templates.TemplateResponse(
        "partials/live_trading.html", {"request": request, "lt": get_live_trading(db)}
    )


@ui_router.get("/partials/calendar", response_class=HTMLResponse)
def partial_calendar(
    request: Request,
    view: str = "month",
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    """PnL calendar partial — month grid (day/week subtotals) or year (per-month) view."""
    ctx = pnlcal.calendar_view(db, view=view, year=year, month=month)
    return templates.TemplateResponse(
        "partials/calendar.html", {"request": request, "ctx": ctx}
    )


@ui_router.get("/partials/calendar/day", response_class=HTMLResponse)
def partial_calendar_day(request: Request, d: str, db: Session = Depends(get_db)):
    """Drilldown: the closed-trade fills for one local calendar day (d=YYYY-MM-DD)."""
    from datetime import date

    try:
        day = date.fromisoformat(d)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date") from exc
    rows = pnlcal.day_fills(db, day)
    total = round(sum(f.realized_pnl for f in rows), 2)
    return templates.TemplateResponse(
        "partials/calendar_day.html",
        {"request": request, "d": d, "rows": rows, "total": total},
    )


@ui_router.get("/partials/kss-settings", response_class=HTMLResponse)
def partial_kss_settings(request: Request, db: Session = Depends(get_db)):
    k = runtime.kss_settings(db)
    # Ladder depth at the last wave = how far below entry the deepest DCA buy sits.
    depth_pct = (1 - (1 - k["scan_distance_pct"] / 100) ** k["scan_max_waves"]) * 100
    from app.orchestrator import grok
    from app.ta import external as ta_external
    grok_scanner = {
        "enabled": settings.grok_scanner_enabled,
        "active": grok.scanner_enabled(),  # enabled AND xai key present
    }
    ta = {
        "lib": settings.ta_lib_enabled,
        "external": settings.ta_external_enabled,
        "external_active": ta_external.enabled(),  # enabled AND taapi key present
    }
    cw = runtime.get_consensus_weights(db)
    return templates.TemplateResponse(
        "partials/kss_settings.html",
        {"request": request, "k": k, "depth_pct": depth_pct, "gs": grok_scanner, "ta": ta,
         "cw": cw},
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
def partial_kss(request: Request, page: int = 1, db: Session = Depends(get_db)):
    page = max(1, min(page, 10))
    offset = (page - 1) * 20
    all_sessions = kss_service.list_sessions(db, limit=offset + 21)
    sessions = all_sessions[offset: offset + 20]
    return templates.TemplateResponse(
        "partials/kss.html",
        {
            "request": request,
            "sessions": sessions,
            "summary": kss_service.summary(db),
            "page": page,
            "has_prev": page > 1,
            "has_next": len(all_sessions) > offset + 20,
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
            "min_expectancy_pct": settings.min_expectancy_pct,
            "min_confidence": settings.min_confidence,
            "deadline_days": settings.deadline_days,
        },
    )


@ui_router.get("/partials/scanner-stats", response_class=HTMLResponse)
def partial_scanner_stats(request: Request, db: Session = Depends(get_db)):
    """S6: last scan timing + cache stats footer partial for the scanner panel."""
    import json as _json

    raw = runtime.get(db, "scanner_last_stats")
    stats: dict = {}
    if raw:
        try:
            stats = _json.loads(raw)
        except (ValueError, TypeError):
            pass
    return templates.TemplateResponse(
        "partials/scanner_stats.html",
        {"request": request, "stats": stats},
    )


@ui_router.get("/partials/performance", response_class=HTMLResponse)
def partial_performance(request: Request, period: str = "all", db: Session = Depends(get_db)):
    if period not in ("24h", "7d", "30d", "all"):
        period = "all"
    p = portfolio.performance_view(db, period=period)
    return templates.TemplateResponse(
        "partials/performance.html",
        {
            "request": request,
            "p": p,
            "period": period,
            "equity_svg": charts.equity_curve_svg(p["equity_curve"], p["equity_times"]),
            "winloss_svg": charts.winloss_bars_svg(p["wins"], p["losses"]),
        },
    )


@ui_router.get("/partials/audit", response_class=HTMLResponse)
def partial_audit(request: Request, category: str = "important", db: Session = Depends(get_db)):
    """The 20 most-recent activity-log events of the requested category (server-side filter,
    so a sparse category is never hidden behind a window of scan noise)."""
    from app import auditview

    if category not in auditview.CATEGORY_LABELS:
        category = "important"
    rows = auditview.recent_by_category(db, category, limit=20)
    return templates.TemplateResponse(
        "partials/audit.html",
        {
            "request": request,
            "rows": rows,
            "category": category,
            "cat_label": auditview.CATEGORY_LABELS[category],
            "filters": list(auditview.CATEGORY_LABELS.items()),
        },
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
