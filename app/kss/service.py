"""
KSS session service — the single source of truth for pyramid session state.

Unlike the original (which kept a parallel in-memory manager dict alongside the
DB), this module rebuilds a `PyramidSession` from DB rows on each operation,
runs the pure strategy logic, then persists the result. The database is the only
state. Generated orders always go through the pending-order approval queue.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app import audit, orders
from app.kss.pyramid import PyramidSession, PyramidSessionStatus, WaveInfo
from app.models import (
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    SESSION_PENDING,
    WAVE_FILLED,
    WAVE_SENT,
    KssSession,
    KssWave,
)

logger = logging.getLogger(__name__)


# --- mapping DB row <-> strategy object ---------------------------------


def _to_pyramid(row: KssSession) -> PyramidSession:
    """Rehydrate a PyramidSession (with its waves and running state) from a DB row."""
    py = PyramidSession(
        symbol=row.symbol,
        entry_price=row.entry_price,
        distance_pct=row.distance_pct,
        max_waves=row.max_waves,
        isolated_fund=row.isolated_fund,
        tp_pct=row.tp_pct,
        timeout_x_min=row.timeout_x_min,
        gap_y_min=row.gap_y_min,
    )
    py.id = row.id
    py.status = PyramidSessionStatus(row.status)
    py.current_wave = row.current_wave
    py.avg_price = row.avg_price
    py.total_filled_qty = row.total_filled_qty
    py.total_cost = row.total_cost
    py.start_time = row.started_at
    py.last_fill_time = row.last_fill_at
    py.created_at = row.created_at
    py.waves = [
        WaveInfo(
            wave_num=w.wave_num,
            quantity=w.quantity,
            target_price=w.target_price,
            status=w.status,
            filled_qty=w.filled_qty or 0.0,
            filled_price=w.filled_price or 0.0,
            filled_time=w.filled_at,
            pending_order_id=w.pending_order_id,
        )
        for w in sorted(row.waves, key=lambda x: x.wave_num)
    ]
    return py


def _save_state(row: KssSession, py: PyramidSession) -> None:
    """Write the strategy's running state back onto the DB row (no commit)."""
    row.status = py.status.value
    row.current_wave = py.current_wave
    row.avg_price = py.avg_price
    row.total_filled_qty = py.total_filled_qty
    row.total_cost = py.total_cost
    row.started_at = py.start_time
    row.last_fill_at = py.last_fill_time


def _get_row(db: Session, session_id: int) -> KssSession:
    row = db.get(KssSession, session_id)
    if row is None:
        raise ValueError(f"Session {session_id} not found")
    return row


def _wave_row(db: Session, session_id: int, wave_num: int) -> KssWave | None:
    return (
        db.query(KssWave)
        .filter(KssWave.session_id == session_id, KssWave.wave_num == wave_num)
        .one_or_none()
    )


# --- lifecycle ----------------------------------------------------------


def create_session(db: Session, **params: Any) -> KssSession:
    """Validate params (via PyramidSession) and persist a PENDING session."""
    from app.config import settings

    note = params.pop("note", None)
    deadline_days = params.pop("deadline_days", settings.deadline_days)
    PyramidSession(**params)  # raises ValueError on invalid params (strategy fields only)
    row = KssSession(status=SESSION_PENDING, note=note, deadline_days=deadline_days, **params)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def start_session(db: Session, session_id: int) -> dict:
    """Start a session: generate wave 0, queue it, persist ACTIVE state."""
    row = _get_row(db, session_id)
    if row.status != SESSION_PENDING:
        raise ValueError(f"Session {session_id} already started (status={row.status})")

    py = _to_pyramid(row)
    order_dict = py.start()
    if not order_dict:
        raise ValueError("Failed to start session (insufficient fund for wave 0?)")

    pending, risk_note = _queue(db, order_dict)
    _save_state(row, py)
    row.deadline_at = datetime.utcnow() + timedelta(days=row.deadline_days)
    db.add(
        KssWave(
            session_id=session_id,
            wave_num=0,
            quantity=order_dict["quantity"],
            target_price=order_dict["price"],
            status=WAVE_SENT,
            pending_order_id=pending.id,
        )
    )
    db.commit()
    return {
        "message": f"Session {session_id} started",
        "deadline_at": row.deadline_at.isoformat(),
        "pending_order_id": pending.id,
        "risk_note": risk_note,
        "order": {k: order_dict[k] for k in ("symbol", "side", "quantity", "price")},
    }


def handle_fill_event(
    db: Session, source_ref: str, filled_qty: float, filled_price: float
) -> dict | None:
    """
    KSS hook invoked by orders.approve_order after a KSS-sourced order fills.

    source_ref formats:
      pyramid:{id}:wave:{n}  -> wave fill -> maybe queue next wave / trigger TP
      pyramid:{id}:tp        -> TP sell filled -> session COMPLETED
    """
    parts = source_ref.split(":")
    if len(parts) < 3 or parts[0] != "pyramid":
        return None
    session_id = int(parts[1])
    row = db.get(KssSession, session_id)
    if row is None:
        return None

    # TP sell completed the session.
    if parts[2] == "tp":
        row.status = SESSION_COMPLETED
        db.commit()
        return {"action": "completed", "message": f"Session {session_id} completed (TP filled)"}

    wave_num = int(parts[3])

    # Mark the filled wave row.
    wave_row = _wave_row(db, session_id, wave_num)
    if wave_row:
        wave_row.status = WAVE_FILLED
        wave_row.filled_qty = filled_qty
        wave_row.filled_price = filled_price
        wave_row.filled_at = datetime.utcnow()

    py = _to_pyramid(row)
    result = py.on_fill(wave_num, filled_qty, filled_price)
    _save_state(row, py)

    if result.get("action") == "next_wave":
        order_dict = result["order"]
        next_wave_num = int(order_dict["source_ref"].split(":")[-1])
        pending, _ = _queue(db, order_dict)
        db.add(
            KssWave(
                session_id=session_id,
                wave_num=next_wave_num,
                quantity=order_dict["quantity"],
                target_price=order_dict["price"],
                status=WAVE_SENT,
                pending_order_id=pending.id,
            )
        )
    elif result.get("action") == "tp_triggered":
        _queue(db, result["order"])  # market SELL through the approval queue

    db.commit()
    return result


def stop_session(db: Session, session_id: int, reason: str = "manual") -> dict:
    row = _get_row(db, session_id)
    if row.status != SESSION_ACTIVE:
        raise ValueError(f"Session {session_id} not active (status={row.status})")
    py = _to_pyramid(row)
    py.stop(reason)
    _save_state(row, py)
    db.commit()
    return {"message": f"Session {session_id} stopped", "reason": reason}


def sweep_deadlines(db: Session, now: datetime | None = None) -> list[int]:
    """
    Force-close ACTIVE sessions past their ≤30-day deadline without TP.

    If the session still holds inventory, a market SELL is queued through the
    normal approval flow (never bypassed). Every close is audit-logged.
    Returns the list of closed session ids.
    """
    now = now or datetime.utcnow()
    overdue = (
        db.query(KssSession)
        .filter(
            KssSession.status == SESSION_ACTIVE,
            KssSession.deadline_at.isnot(None),
            KssSession.deadline_at < now,
        )
        .all()
    )
    closed: list[int] = []
    for row in overdue:
        py = _to_pyramid(row)
        py.stop("deadline")
        _save_state(row, py)
        if row.total_filled_qty > 0:
            _queue(db, {
                "symbol": row.symbol, "side": "SELL", "quantity": row.total_filled_qty,
                "price": 0.0, "order_type": "MARKET",
                "source_ref": f"pyramid:{row.id}:deadline",
                "strategy_name": f"Pyramid_{row.symbol}",
                "note": f"Deadline close after {row.deadline_days}d",
            })
        audit.log(db, "scheduler", "deadline_close", entity=f"kss:{row.id}",
                  symbol=row.symbol, deadline_days=row.deadline_days)
        closed.append(row.id)
    db.commit()
    return closed


def adjust_session(db: Session, session_id: int, **changes: Any) -> dict:
    row = _get_row(db, session_id)
    py = _to_pyramid(row)
    applied = py.adjust_params(**{k: v for k, v in changes.items() if v is not None})
    if not applied:
        raise ValueError("No valid changes applied")
    # persist adjustable parameters
    for field, value in applied.items():
        setattr(row, field, value)
    db.commit()
    return {"message": f"Session {session_id} adjusted", "changes": applied}


def delete_session(db: Session, session_id: int) -> dict:
    row = _get_row(db, session_id)
    if row.status == SESSION_ACTIVE:
        raise ValueError("Cannot delete an active session; stop it first")
    db.delete(row)
    db.commit()
    return {"message": f"Session {session_id} deleted"}


def check_tp(db: Session, session_id: int, current_price: float | None = None) -> dict:
    """Manually evaluate the TP condition; queue the TP sell if triggered."""
    row = _get_row(db, session_id)
    py = _to_pyramid(row)
    if py.total_filled_qty <= 0:
        return {"tp_triggered": False, "message": "No filled quantity yet"}
    result = py.check_tp(current_price) if current_price is not None else None
    if current_price is None:
        from app.market import get_current_prices

        price = get_current_prices([row.symbol]).get(row.symbol, 0.0)
        result = py.check_tp(price)
        current_price = price
    payload = {
        "tp_triggered": bool(result),
        "current_price": current_price,
        "avg_price": py.avg_price,
        "tp_price": py.estimated_tp_price,
    }
    if result:
        _queue(db, result["order"])
        _save_state(row, py)
        db.commit()
        payload["tp_order_queued"] = True
    return payload


# --- reads --------------------------------------------------------------


def get_status(db: Session, session_id: int) -> dict:
    return _to_pyramid(_get_row(db, session_id)).get_status()


def list_sessions(
    db: Session, status: str | None = None, symbol: str | None = None, limit: int = 100
) -> list[dict]:
    q = db.query(KssSession)
    if status:
        q = q.filter(KssSession.status == status)
    if symbol:
        q = q.filter(KssSession.symbol == symbol)
    rows = q.order_by(KssSession.created_at.desc()).limit(limit).all()
    return [_to_pyramid(r).get_status() for r in rows]


def summary(db: Session) -> dict:
    rows = db.query(KssSession).all()
    active = [r for r in rows if r.status == SESSION_ACTIVE]
    return {
        "total_sessions": len(rows),
        "active_sessions": len(active),
        "total_isolated_fund": sum(r.isolated_fund for r in active),
        "total_used_fund": sum(r.total_cost for r in rows),
    }


def preview(
    symbol: str,
    entry_price: float,
    distance_pct: float,
    max_waves: int,
    isolated_fund: float,
    tp_pct: float,
) -> dict:
    """
    Equal-qty / linear-price projection used by the dashboard's "Preview Pyramid".

    NOTE: intentionally simpler than the live geometric waves (see kss-spec skill).
    """
    qty_per_wave = isolated_fund / max_waves / entry_price
    waves = []
    cum_qty = 0.0
    cum_cost = 0.0
    for n in range(max_waves):
        target = entry_price * (1 - distance_pct / 100 * n)
        cum_qty += qty_per_wave
        cum_cost += qty_per_wave * target
        avg_after = cum_cost / cum_qty if cum_qty > 0 else 0.0
        waves.append(
            {
                "wave_num": n,
                "target_price": round(target, 8),
                "quantity": round(qty_per_wave, 8),
                "cumulative_qty": round(cum_qty, 8),
                "cumulative_cost": round(cum_cost, 4),
                "avg_price_after": round(avg_after, 8),
                "tp_price_after": round(avg_after * (1 + tp_pct / 100), 8) if avg_after > 0 else 0.0,
            }
        )
    final_wave_price = entry_price * (1 - distance_pct / 100 * (max_waves - 1))
    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "distance_pct": distance_pct,
        "max_waves": max_waves,
        "isolated_fund": isolated_fund,
        "tp_pct": tp_pct,
        "qty_per_wave": round(qty_per_wave, 8),
        "waves": waves,
        "total_qty": round(cum_qty, 8),
        "total_cost": round(cum_cost, 4),
        "final_avg_price": round(cum_cost / cum_qty, 8) if cum_qty > 0 else 0.0,
        "final_tp_price": waves[-1]["tp_price_after"] if waves else 0.0,
        "price_range_pct": round((entry_price - final_wave_price) / entry_price * 100, 2),
    }


# --- internal -----------------------------------------------------------


def _queue(db: Session, order_dict: dict):
    """Route a strategy-generated order through the pending-order approval queue."""
    return orders.queue_order(
        db,
        symbol=order_dict["symbol"],
        side=order_dict["side"],
        quantity=order_dict["quantity"],
        price=order_dict.get("price", 0.0),
        order_type=order_dict.get("order_type", "LIMIT"),
        source="kss",
        source_ref=order_dict["source_ref"],
        strategy_name=order_dict.get("strategy_name"),
        note=order_dict.get("note"),
    )
