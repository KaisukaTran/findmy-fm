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

from sqlalchemy.orm import Session

from app import audit, costengine, market, orders, runtime
from app.clock import utcnow
from app.config import settings
from app.orchestrator import brain, service
from app.orchestrator.models import OPUS_CLOSED, OPUS_RIDE, OPUS_WATCH, OpusPosition

log = logging.getLogger(__name__)


def _candidate_symbols(db: Session) -> set[str]:
    return {c["symbol"] for c in brain._candidates(db, k=25)}


def _candidate_consensus(db: Session, symbol: str) -> float | None:
    """Latest scan's deterministic consensus % for `symbol`, or None if there's no matching
    row (mirrors brain.py's `_candidates` latest-ScanRun query, narrowed to one symbol). In
    production a symbol that passed the `_candidate_symbols` check always has a row here
    (same query); None only happens defensively (e.g. a scan-less test fixture) and skips
    the floor below rather than blocking on data that doesn't exist."""
    from app.models import Candidate, ScanRun
    scan = db.query(ScanRun).order_by(ScanRun.id.desc()).first()
    if not scan:
        return None
    row = (
        db.query(Candidate)
        .filter(Candidate.scan_id == scan.id, Candidate.symbol == symbol)
        .first()
    )
    return row.consensus_pct if row else None


def _open(db: Session, intent: dict, allowed: set[str], result: dict) -> None:
    symbol = intent.get("symbol")
    notional = intent.get("notional")
    # Anti-injection: only symbols the scanner actually surfaced may be opened.
    if not symbol or symbol not in allowed:
        result["rejected"].append({"intent": intent, "reason": "symbol not a current candidate"})
        return
    # P2: deterministic consensus floor, cage-side (not prompt-side) — applies to EVERY
    # open regardless of who proposed it (solo or agreed). Trial sets the floor to 0 so
    # this never rejects; a live cage can raise it without touching the prompt.
    consensus_pct = _candidate_consensus(db, symbol)
    if consensus_pct is not None and consensus_pct < settings.opus_solo_min_consensus:
        result["rejected"].append({
            "intent": intent,
            "reason": f"consensus {consensus_pct:.1f} below solo floor {settings.opus_solo_min_consensus:.1f}",
        })
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
        symbol=symbol, opened_at=utcnow(), entry_price=price, qty=qty,
        avg_price=price, state=OPUS_WATCH, watch_started_at=utcnow(),
    )
    db.add(pos)
    db.flush()  # assign id for the source_ref

    order, _ = orders.queue_order(
        db, symbol=symbol, side="BUY", quantity=qty, price=0.0, order_type="MARKET",
        source="opus", source_ref=f"opus:{pos.id}:open", strategy_name="OPUS",
        note=(intent.get("reason") or "")[:200],
    )
    try:
        fill = orders.approve_order(db, order.id, reviewer="opus")
    except orders.InsufficientCashError:
        # Not enough cash to open — drop the just-created watch row, skip this intent.
        db.delete(pos)
        db.flush()
        audit.log(db, "opus", "open_underfunded", entity=symbol, symbol=symbol)
        return
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
        pos.closed_at = utcnow()
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
    pos.closed_at = utcnow()
    audit.log(db, "opus", "close", entity=f"opos:{pos.id}", symbol=pos.symbol,
              realized=round(realized, 4), reason=reason)
    db.commit()
    return realized


def _reduce(db: Session, intent: dict, result: dict) -> None:
    """Partial take-profit: sell `notional` USD worth of an open position, banking profit
    while letting the remainder ride. If what's left after the sale would be below min
    notional (unsellable dust), escalate to a FULL close instead — never strand a tail no
    order could ever fill."""
    pid = intent.get("position_id")
    notional = intent.get("notional")
    pos = db.get(OpusPosition, pid) if isinstance(pid, int) else None
    if pos is None or pos.state not in {OPUS_WATCH, OPUS_RIDE}:
        result["rejected"].append({"intent": intent, "reason": "position not open/Opus-managed"})
        return
    if not isinstance(notional, (int, float)) or notional <= 0:
        result["rejected"].append({"intent": intent, "reason": "missing/invalid notional"})
        return

    price = market.get_current_prices([pos.symbol]).get(pos.symbol) or 0.0
    if price <= 0:
        result["rejected"].append({"intent": intent, "reason": "no price"})
        return

    sell_qty = min(float(notional) / price, pos.qty)
    remainder_notional = (pos.qty - sell_qty) * price
    if not costengine.notional_ok(remainder_notional):
        # The remainder would be dust no order could fill — close the whole position instead
        # of stranding it.
        realized = force_close(db, pos, "reduce→close (remainder below min notional)")
        result["executed"].append({"action": "close", "position_id": pos.id, "realized": realized})
        return

    order, _ = orders.queue_order(
        db, symbol=pos.symbol, side="SELL", quantity=sell_qty, price=0.0, order_type="MARKET",
        source="opus", source_ref=f"opus:{pos.id}:reduce", strategy_name="OPUS",
        note=(intent.get("reason") or "")[:200],
    )
    fill = orders.approve_order(db, order.id, reviewer="opus")
    pos.qty -= fill.quantity
    pos.realized_pnl = (pos.realized_pnl or 0.0) + (fill.realized_pnl or 0.0)
    audit.log(db, "opus", "reduce", entity=f"opos:{pos.id}", symbol=pos.symbol,
              qty=round(fill.quantity, 8), realized=round(fill.realized_pnl or 0.0, 4),
              reason=intent.get("reason"))
    result["executed"].append({"action": "reduce", "position_id": pos.id,
                               "qty": round(fill.quantity, 8)})


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

    # P2: daily-loss stop only blocks NEW opens; closes/reduces still run (risk reduction
    # never waits for the brake to lift). Logged once per batch, not per intent.
    daily_stop = service.daily_loss_stop_active(db)
    if daily_stop:
        audit.log(db, "opus", "skipped_daily_loss_stop",
                  n=sum(1 for it in intents if it.get("action") == "open"))

    allowed = _candidate_symbols(db)
    for it in intents:
        try:
            if it["action"] == "open":
                if daily_stop:
                    result["rejected"].append({"intent": it, "reason": "daily_loss_stop"})
                    continue
                _open(db, it, allowed, result)
            elif it["action"] == "close":
                _close(db, it, result)
            elif it["action"] == "reduce":
                _reduce(db, it, result)
            # 'hold' → nothing
        except Exception as exc:  # one bad intent must not abort the batch
            log.warning("OPUS intent failed (%s): %s", it.get("action"), type(exc).__name__)
            result["rejected"].append({"intent": it, "reason": type(exc).__name__})
    db.commit()
    return result
