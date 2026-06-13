"""
OPUS sandbox — the deterministic cage around Opus's advice (O-3).

`apply_intents` is the ONLY path from an Opus intent to an order. It re-validates every
intent against hard caps and the capital envelope, clamps sizing, and routes survivors
through the existing approval queue (reviewer="opus", so the circuit breaker blocks them
when frozen). A prompt-injected/hallucinating Opus cannot exceed a cap, touch non-OPUS
capital, or trade a symbol the scanner didn't surface. Shadow mode logs intents without
executing. Paper-only.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app import audit, costengine, market, orders, runtime
from app.config import settings
from app.orchestrator import brain, service
from app.orchestrator.models import OPUS_CLOSED, OPUS_RIDE, OPUS_WATCH, OpusPosition

log = logging.getLogger(__name__)


def _candidate_symbols(db: Session) -> set[str]:
    return {c["symbol"] for c in brain._candidates(db, k=25)}


def _open(db: Session, intent: dict, allowed: set[str], result: dict) -> None:
    symbol = intent.get("symbol")
    notional = intent.get("notional")
    # Anti-injection: only symbols the scanner actually surfaced may be opened.
    if not symbol or symbol not in allowed:
        result["rejected"].append({"intent": intent, "reason": "symbol not a current candidate"})
        return
    # K-1 strategy exclusivity: never open a coin KSS already runs (no blended cost basis).
    from app.models import SESSION_ACTIVE, KssSession
    if db.query(KssSession).filter(
        KssSession.symbol == symbol, KssSession.status == SESSION_ACTIVE
    ).count() > 0:
        result["rejected"].append({"intent": intent, "reason": "coin has an active KSS session"})
        return
    # One OPUS lot per coin: don't stack a second position on a symbol we already hold.
    # Two lots → two 3h rescues → two KSS sessions blending one Position's cost basis (K-1).
    if db.query(OpusPosition).filter(
        OpusPosition.symbol == symbol, OpusPosition.state.in_((OPUS_WATCH, OPUS_RIDE))
    ).count() > 0:
        result["rejected"].append({"intent": intent, "reason": "OPUS already holds this coin"})
        return
    if not isinstance(notional, (int, float)) or notional <= 0:
        result["rejected"].append({"intent": intent, "reason": "missing/invalid notional"})
        return

    price = market.get_current_prices([symbol]).get(symbol) or 0.0
    if price <= 0:
        result["rejected"].append({"intent": intent, "reason": "no price"})
        return

    # Clamp to per-trade cap and remaining envelope; reject dust below min notional.
    free = max(0.0, service.allocation() - service.deployed(db))
    capped = min(float(notional), settings.opus_max_trade_notional, free)
    if not costengine.notional_ok(capped):
        result["rejected"].append({"intent": intent, "reason": f"below min notional (free=${free:.2f})"})
        return

    qty = capped / price
    pos = OpusPosition(
        symbol=symbol, opened_at=datetime.utcnow(), entry_price=price, qty=qty,
        avg_price=price, state=OPUS_WATCH, watch_started_at=datetime.utcnow(),
    )
    db.add(pos)
    db.flush()  # assign id for the source_ref

    order, _ = orders.queue_order(
        db, symbol=symbol, side="BUY", quantity=qty, price=0.0, order_type="MARKET",
        source="opus", source_ref=f"opus:{pos.id}:open", strategy_name="OPUS",
        note=(intent.get("reason") or "")[:200],
    )
    fill = orders.approve_order(db, order.id, reviewer="opus")
    pos.qty = fill.quantity
    pos.avg_price = fill.price
    pos.entry_price = fill.price
    audit.log(db, "opus", "open", entity=f"opos:{pos.id}", symbol=symbol,
              notional=round(capped, 2), price=fill.price, reason=intent.get("reason"))
    result["executed"].append({"action": "open", "position_id": pos.id, "symbol": symbol,
                               "notional": round(capped, 2)})


def force_close(db: Session, pos: OpusPosition, reason: str) -> float | None:
    """SELL the whole position through the queue and mark it closed. Returns realized PnL
    (None if it couldn't sell, e.g. breaker frozen). Used by close intents and the ride
    hard-stop. Commits."""
    if pos.qty <= 0:
        pos.state = OPUS_CLOSED
        pos.closed_at = datetime.utcnow()
        db.commit()
        return 0.0
    order, _ = orders.queue_order(
        db, symbol=pos.symbol, side="SELL", quantity=pos.qty, price=0.0, order_type="MARKET",
        source="opus", source_ref=f"opus:{pos.id}:close", strategy_name="OPUS",
        note=reason[:200],
    )
    fill = orders.approve_order(db, order.id, reviewer="opus")  # raises if frozen
    realized = fill.realized_pnl or 0.0
    pos.realized_pnl = (pos.realized_pnl or 0.0) + realized
    pos.state = OPUS_CLOSED
    pos.closed_at = datetime.utcnow()
    audit.log(db, "opus", "close", entity=f"opos:{pos.id}", symbol=pos.symbol,
              realized=round(realized, 4), reason=reason)
    db.commit()
    return realized


def _close(db: Session, intent: dict, result: dict) -> None:
    pid = intent.get("position_id")
    pos = db.get(OpusPosition, pid) if isinstance(pid, int) else None
    if pos is None or pos.state not in {OPUS_WATCH, OPUS_RIDE}:
        result["rejected"].append({"intent": intent, "reason": "position not open/Opus-managed"})
        return
    realized = force_close(db, pos, intent.get("reason") or "opus close")
    result["executed"].append({"action": "close", "position_id": pos.id, "realized": realized})


def apply_intents(db: Session, intents: list[dict]) -> dict:
    """Validate/clamp/route intents. Returns {executed, rejected, shadow}. Never raises."""
    result: dict = {"executed": [], "rejected": [], "shadow": bool(settings.opus_shadow)}

    if settings.opus_shadow:
        for it in intents:
            audit.log(db, "opus", "shadow_intent", intent_action=it.get("action"),
                      symbol=it.get("symbol"), notional=it.get("notional"))
        result["rejected"] = [{"intent": it, "reason": "shadow"} for it in intents]
        db.commit()
        return result

    if runtime.is_frozen(db):
        audit.log(db, "opus", "skipped_frozen", n=len(intents))
        result["rejected"] = [{"intent": it, "reason": "frozen"} for it in intents]
        return result

    allowed = _candidate_symbols(db)
    for it in intents:
        try:
            if it["action"] == "open":
                _open(db, it, allowed, result)
            elif it["action"] == "close":
                _close(db, it, result)
            # 'hold' → nothing
        except Exception as exc:  # one bad intent must not abort the batch
            log.warning("OPUS intent failed (%s): %s", it.get("action"), type(exc).__name__)
            result["rejected"].append({"intent": it, "reason": type(exc).__name__})
    db.commit()
    return result
