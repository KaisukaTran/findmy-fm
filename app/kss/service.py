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
    SESSION_STOPPED,
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
    from app.config import settings

    py.id = row.id
    py.status = PyramidSessionStatus(row.status)
    py.current_wave = row.current_wave
    py.avg_price = row.avg_price
    py.total_filled_qty = row.total_filled_qty
    py.total_cost = row.total_cost
    py.start_time = row.started_at
    py.last_fill_time = row.last_fill_at
    py.created_at = row.created_at
    py.sl_pct = row.sl_pct if row.sl_pct > 0 else settings.sl_pct
    py.trailing_pct = row.trailing_pct if row.trailing_pct > 0 else settings.trailing_pct
    py.peak_price = row.peak_price
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
    row.peak_price = py.peak_price


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


def _sl_floor_price(py: PyramidSession) -> float:
    """Hard stop-loss trigger price (avg-anchored) for an in-flight session, or 0.0 when SL is
    disabled. A DCA wave at/below this can never execute — the SL exits first — so queueing it
    would make the ladder and the SL mutually contradictory. Callers skip such dead rungs."""
    if py.sl_pct <= 0 or py.avg_price <= 0:
        return 0.0
    return py.avg_price * (1 - py.sl_pct / 100.0)


def _anchor_dca_price(
    db: Session, session_id: int, symbol: str, distance_pct: float,
    fallback_price: float, entry_price: float,
) -> float:
    """Re-anchor a DCA BUY rung to the LIVE market so it is NEVER queued above the current
    price. An entry-anchored geometric rung (``entry×(1−d)ⁿ``) drifts ABOVE the market after a
    fast drop; a BUY limit above market fills immediately at an OVERPAY instead of averaging
    down (seen live: STG wave 5 filled 0.2257 while the market was 0.2123). Returns
    ``min(current_market, previous_wave_price) × (1 − distance%)`` — the wave-step % below the
    lower of the current price and the previous wave (so it is below BOTH). When no live price
    is available (offline) ``min`` falls back to the previous wave → ``prev × (1−d)`` ≈ the
    geometric rung, preserving the legacy ladder. Shared by manual DCA+ and the auto-chain."""
    from app.market import get_current_prices

    mkt = get_current_prices([symbol]).get(symbol) or 0.0
    last = (
        db.query(KssWave)
        .filter(KssWave.session_id == session_id)
        .order_by(KssWave.wave_num.desc())
        .first()
    )
    prev = last.target_price if last is not None else entry_price
    anchors = [p for p in (mkt, prev) if p > 0]
    if not anchors:
        return fallback_price
    return round(min(anchors) * (1 - distance_pct / 100.0), 8)


def _queue_wave_if_above_sl(
    db: Session, py: PyramidSession, session_id: int, symbol: str, order_dict: dict
) -> bool:
    """Queue the next DCA wave (+ its KssWave row) and return True, or skip+audit and return
    False when the rung sits at/below the SL — a dead order the SL would pre-empt. Shared by the
    auto-chain (handle_fill_event) and the rescue adoption so both honour the SL floor.

    The rung is first re-anchored to the live market (``_anchor_dca_price``) so the auto-chain
    never queues a buy above the current price (which would fill at an overpay, not a dip)."""
    nwn = int(order_dict["source_ref"].split(":")[-1])
    order_dict["price"] = _anchor_dca_price(
        db, session_id, symbol, py.distance_pct, order_dict["price"], py.entry_price
    )
    floor = _sl_floor_price(py)
    if floor > 0 and order_dict["price"] <= floor:
        audit.log(db, "kss", "wave_below_sl", entity=f"kss:{session_id}", symbol=symbol,
                  wave=nwn, price=round(order_dict["price"], 8), sl_price=round(floor, 8))
        return False
    pending, _ = _queue(db, order_dict)
    db.add(
        KssWave(
            session_id=session_id, wave_num=nwn, quantity=order_dict["quantity"],
            target_price=order_dict["price"], status=WAVE_SENT, pending_order_id=pending.id,
        )
    )
    return True


# --- lifecycle ----------------------------------------------------------


def projected_ladder_cost(
    symbol: str,
    entry_price: float,
    distance_pct: float,
    max_waves: int,
) -> float:
    """USD a session's FULL DCA ladder would consume, from the frozen pyramid math:
    Σ over waves of (wave qty × wave price) via ``PyramidSession.estimate_total_cost``.

    This is the precise form of "first-wave size × số sóng × giá": it honours the
    geometric (n+1)× qty growth, the entry×(1−d)ⁿ price decay, and the active
    ``kss_first_wave_usd`` sizing. Used to (1) size a session's ``isolated_fund`` so the
    ladder never starves mid-way, and (2) budget the scanner open-gate against the real
    planned capital instead of a flat user-set ``scan_fund``.
    """
    probe = PyramidSession(
        symbol=symbol,
        entry_price=entry_price,
        distance_pct=distance_pct,
        max_waves=max_waves,
        isolated_fund=1.0,  # dummy (>0 to pass validation); estimate_total_cost ignores it
        tp_pct=1.0,
        timeout_x_min=1.0,
        gap_y_min=0.0,
    )
    return probe.estimate_total_cost()


def create_session(db: Session, **params: Any) -> KssSession:
    """Validate params (via PyramidSession) and persist a PENDING session."""
    from app import costengine
    from app.config import settings

    note = params.pop("note", None)
    deadline_days = params.pop("deadline_days", settings.deadline_days)
    # Cost floor: never take profit on a gain that wouldn't clear 2x the highest
    # Binance fee. Raise tp_pct to the floor (the frozen TP math is left untouched).
    floor = costengine.min_profit_pct()
    if params.get("tp_pct", 0.0) < floor:
        logger.info(
            "tp_pct %.4f below profit floor %.4f%% — raising to floor",
            params.get("tp_pct"), floor,
        )
        params["tp_pct"] = floor
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


def adopt_position_into_kss(
    db: Session,
    symbol: str,
    held_qty: float,
    avg_price: float,
    current_price: float,
    *,
    note: str = "opus-rescue",
) -> KssSession:
    """
    Wrap an ALREADY-HELD position into an ACTIVE KSS session (OPUS 3h rescue, docs §5).

    Reuses the frozen pyramid math: seed wave 0 as *filled* with the held qty/avg (no new
    buy), then let on_fill() queue the next DCA wave and persist state. From here the normal
    KSS rules (DCA ladder, SL/trailing, TP, deadline) govern the losing trade.

    K-1: at most one ACTIVE KSS session per symbol (one owner → session avg == the symbol
    Position avg → no 'take-profit that realizes a loss'). If this coin already has an owner,
    fold the rescued lot into it (`_merge_rescue`) instead of opening a parallel session.
    """
    from app.config import settings

    existing = (
        db.query(KssSession)
        .filter(KssSession.symbol == symbol, KssSession.status == SESSION_ACTIVE)
        .order_by(KssSession.created_at.asc())
        .first()
    )
    if existing is not None:
        return _merge_rescue(db, existing, held_qty, avg_price, current_price, note=note)

    held_notional = max(held_qty * avg_price, 0.0)
    row = create_session(
        db,
        symbol=symbol,
        entry_price=avg_price,
        distance_pct=settings.scan_distance_pct,
        max_waves=settings.scan_max_waves,
        # room for the held lot plus a bounded DCA-down budget
        isolated_fund=held_notional + settings.scan_fund,
        tp_pct=settings.scan_tp_pct,
        timeout_x_min=float(settings.deadline_days * 1440),
        gap_y_min=0.0,
        deadline_days=settings.deadline_days,
        note=note,
    )

    py = _to_pyramid(row)
    py.start()  # marks ACTIVE + appends wave 0 as "sent" (we do NOT queue this buy)
    result = py.on_fill(0, held_qty, avg_price, current_market_price=current_price)
    _save_state(row, py)
    row.deadline_at = datetime.utcnow() + timedelta(days=row.deadline_days)
    row.last_fill_at = datetime.utcnow()

    db.add(
        KssWave(
            session_id=row.id, wave_num=0, quantity=held_qty,
            target_price=avg_price, status=WAVE_FILLED,
            filled_qty=held_qty, filled_price=avg_price, filled_at=datetime.utcnow(),
        )
    )
    # If the strategy wants the next DCA wave, queue it like a normal fill would — unless the
    # rung is at/below the SL (dead: the SL would exit before it could fill).
    if result.get("action") == "next_wave":
        nwn = int(result["order"]["source_ref"].split(":")[-1])
        if not _queue_wave_if_above_sl(db, py, row.id, symbol, result["order"]):
            row.current_wave = nwn - 1  # frozen on_fill advanced it; nothing was queued
    elif result.get("action") == "tp_triggered":
        if _tp_clears_cost(db, row.symbol, current_price):
            _queue(db, result["order"])
        else:
            row.status = SESSION_ACTIVE  # K-2 defer on adoption

    db.commit()
    audit.log(db, "opus", "kss_rescue", entity=f"kss:{row.id}", symbol=symbol,
              held_qty=held_qty, avg=avg_price)
    return row


def _merge_rescue(
    db: Session, row: KssSession, held_qty: float, avg_price: float, current_price: float,
    *, note: str,
) -> KssSession:
    """
    Fold an OPUS-rescued lot into an EXISTING active session (K-1: one owner per coin).

    Appends the held lot as a final filled wave so the frozen `on_fill` recomputes the
    session avg/cost over the whole inventory. The ladder is capped at this wave so no new
    DCA buy is queued — from here the session's TP/SL/deadline manage the combined position.
    Avoids the parallel-session bug where two sessions split one symbol's Position (blended
    cost basis → a 'take-profit' that realises a loss).
    """
    py = _to_pyramid(row)
    wave_num = max((w.wave_num for w in py.waves), default=-1) + 1
    py.max_waves = wave_num + 1  # cap so on_fill records the fill but queues nothing further
    py.isolated_fund += max(held_qty * avg_price, 0.0)  # room for the merged notional
    py.waves.append(
        WaveInfo(wave_num=wave_num, quantity=held_qty, target_price=avg_price, status="sent")
    )
    py.on_fill(wave_num, held_qty, avg_price, current_market_price=current_price)
    if py.status == PyramidSessionStatus.TP_TRIGGERED:
        # A merge must never auto-sell; let the scheduler's K-2-guarded TP decide next tick.
        py.status = PyramidSessionStatus.ACTIVE
    _save_state(row, py)
    row.max_waves = py.max_waves
    row.isolated_fund = py.isolated_fund
    row.last_fill_at = datetime.utcnow()
    db.add(
        KssWave(
            session_id=row.id, wave_num=wave_num, quantity=held_qty,
            target_price=avg_price, status=WAVE_FILLED,
            filled_qty=held_qty, filled_price=avg_price, filled_at=datetime.utcnow(),
        )
    )
    db.commit()
    audit.log(db, "opus", "kss_rescue_merge", entity=f"kss:{row.id}", symbol=row.symbol,
              held_qty=held_qty, avg=avg_price, note=note)
    return row


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

    # Stop-loss / trailing-stop sells terminate the session as STOPPED.
    if parts[2] in {"sl", "trailing", "deadline"}:
        row.status = SESSION_STOPPED
        # Record a re-entry cooldown after a risk exit (not a calendar deadline),
        # so the scanner doesn't immediately re-open the same falling symbol.
        if parts[2] in {"sl", "trailing"}:
            from app import runtime

            runtime.set(db, f"stop_cooldown:{row.symbol}", datetime.utcnow().isoformat())
        db.commit()
        return {"action": parts[2], "message": f"Session {session_id} stopped ({parts[2]})"}

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
        if not _queue_wave_if_above_sl(db, py, session_id, row.symbol, result["order"]):
            row.current_wave = wave_num  # frozen on_fill advanced it; nothing was queued
    elif result.get("action") == "tp_triggered":
        _handle_tp_triggered(db, row, result)

    db.commit()
    return result


def _handle_tp_triggered(db: Session, row: KssSession, result: dict) -> None:
    """Queue the TP sell, or K-2 defer it (back to ACTIVE) when selling now would realize
    below the true cost basis + fees."""
    from app.market import get_current_prices

    mkt = get_current_prices([row.symbol]).get(row.symbol) or 0.0
    if mkt and not _tp_clears_cost(db, row.symbol, mkt):
        row.status = SESSION_ACTIVE  # K-2 defer (on_fill had set TP_TRIGGERED)
        audit.log(db, "kss", "tp_deferred", entity=f"kss:{row.id}", symbol=row.symbol, price=mkt)
    else:
        _queue(db, result["order"])  # market SELL through the approval queue


def _idle_deployable(db: Session) -> float:
    """Free USDT cash available to deploy RIGHT NOW — used by a manual DCA+ to fund a wave
    beyond a session's ``isolated_fund`` reservation (the reservation is a planning cap, not
    real set-aside cash). This is exactly the ``cash`` the portfolio summary shows:
    ``account_equity − cost of open positions + realized PnL``.

    It is the REAL free balance, NOT reduced by the ``equity_backup_pct`` reserve: that reserve
    gates the AUTO scanner's new-session opens, whereas a manual DCA+ is the user deliberately
    choosing to deploy their idle cash now. (The old formula subtracted the full cost basis from
    a 75%-of-equity budget, which wrongly returned 0 whenever a lot was already deployed even
    with real cash sitting idle.)"""
    from sqlalchemy import func

    from app.config import settings
    from app.models import Fill, Position

    invested = sum(p.total_cost for p in db.query(Position).all())
    realized = float(db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0)
    return max(0.0, settings.account_equity - invested + realized)


def queue_next_wave(db: Session, session_id: int, amount_usd: float | None = None) -> dict:
    """
    Manually queue the next DCA wave for an ACTIVE session (the DCA+ button).

    Bootstraps DCA when the wave chain has gone dormant — e.g. after extending `max_waves`
    on a session whose ladder was already exhausted (all waves filled, nothing pending). The
    next wave is the standard geometric rung (`generate_wave(current_wave + 1)`); once it
    fills, the frozen `on_fill` auto-chains the rest. Goes through the approval queue.

    Manual override (user intent): the per-session ``isolated_fund`` reservation is a PLANNING
    cap, not real cash. When the next rung costs more than this session has left reserved, fund
    it from idle account cash (``_idle_deployable``) and grow ``isolated_fund`` to match — so a
    deliberate DCA+ click deploys free capital now instead of being blocked by the reservation.
    ``_idle_deployable`` is the REAL free balance and is NOT reduced by the ``equity_backup_pct``
    reserve: a manual DCA+ is the user deliberately choosing to spend remaining cash (incl. the
    auto-backup) — exactly the lever the backup exists for. The AUTO path (handle_fill_event /
    scanner) is untouched and still honours the backup + lend-idle budget.

    ``amount_usd`` (optional, manual lever): deploy a user-chosen USD slice this wave
    (qty = amount/anchored price) instead of the fixed geometric rung — so the user can put a
    meaningful amount of idle cash to work on demand. A custom-amount click also extends the
    ladder by one when it is full (a deliberate deploy must not be blocked by ``max_waves``).
    """
    row = _get_row(db, session_id)
    if row.status != SESSION_ACTIVE:
        raise ValueError(f"Session {session_id} not active (status={row.status})")
    if amount_usd is not None and amount_usd <= 0:
        raise ValueError("amount_usd phải > 0")
    py = _to_pyramid(row)
    next_wave_num = py.current_wave + 1
    if next_wave_num >= py.max_waves:
        # A plain DCA+ refuses on a full ladder (raise max_waves first); a deliberate custom-USD
        # deploy extends the ladder by one so the user is never blocked from putting cash to work.
        if amount_usd is None:
            raise ValueError(
                f"Ladder exhausted (current_wave={py.current_wave}, max_waves={py.max_waves}); "
                "raise max_waves first"
            )
        py.max_waves = next_wave_num + 1
        row.max_waves = py.max_waves
        audit.log(db, "kss", "dca_extend_ladder", entity=f"kss:{session_id}",
                  symbol=row.symbol, max_waves=py.max_waves)
    if _wave_row(db, session_id, next_wave_num) is not None:
        raise ValueError(f"Wave {next_wave_num} already queued")
    next_wave = py.generate_wave(next_wave_num)
    # User rule: a DCA+ rung must sit BELOW the live market by the step %, AND below the
    # previous wave — never above market (see _anchor_dca_price).
    next_wave.target_price = _anchor_dca_price(
        db, session_id, row.symbol, py.distance_pct, next_wave.target_price, py.entry_price
    )
    if amount_usd is not None and next_wave.target_price > 0:
        # Size the wave to deploy the chosen USD at the anchored price (manual override).
        next_wave.quantity = round(amount_usd / next_wave.target_price, 8)
    floor = _sl_floor_price(py)
    if floor > 0 and next_wave.target_price <= floor:
        raise ValueError(
            f"Sóng {next_wave_num} (giá {next_wave.target_price:.6f}) nằm dưới SL "
            f"({floor:.6f}) — SL sẽ kích hoạt trước nên lệnh DCA này vô dụng. "
            "Nới SL của session hoặc giảm max_waves trước."
        )
    cost = next_wave.quantity * next_wave.target_price
    if cost > py.remaining_fund:
        # Reservation exhausted — fund this rung from idle account cash (manual override).
        idle = _idle_deployable(db)
        if cost > idle:
            raise ValueError(
                f"Không đủ tiền mặt cho sóng {next_wave_num}: cần {cost:.2f}, "
                f"tiền nhàn rỗi {idle:.2f}. Đóng/giảm bớt session khác để giải phóng vốn."
            )
        added = cost - py.remaining_fund
        row.isolated_fund = py.total_cost + cost  # grow the reservation to fund this wave now
        py.isolated_fund = row.isolated_fund
        audit.log(db, "kss", "dca_fund_topup", entity=f"kss:{session_id}", symbol=row.symbol,
                  wave=next_wave_num, added=round(added, 2),
                  new_isolated_fund=round(row.isolated_fund, 2))
    py.current_wave = next_wave_num
    next_wave.status = "sent"
    pending, risk_note = _queue(db, py._wave_to_order(next_wave))
    _save_state(row, py)
    db.add(
        KssWave(
            session_id=session_id, wave_num=next_wave_num, quantity=next_wave.quantity,
            target_price=next_wave.target_price, status=WAVE_SENT, pending_order_id=pending.id,
        )
    )
    audit.log(db, "kss", "dca_next", entity=f"kss:{session_id}", symbol=row.symbol,
              wave=next_wave_num, price=next_wave.target_price, qty=next_wave.quantity,
              amount_usd=round(amount_usd, 2) if amount_usd is not None else None)
    db.commit()
    return {
        "message": f"Queued wave {next_wave_num} @ {next_wave.target_price}",
        "wave_num": next_wave_num,
        "price": next_wave.target_price,
        "quantity": next_wave.quantity,
        "cost": round(next_wave.quantity * next_wave.target_price, 2),
        "pending_order_id": pending.id,
        "risk_note": risk_note,
    }


def consolidate_sessions(db: Session, keep_id: int, merge_id: int) -> dict:
    """
    Merge a duplicate session's inventory into another session for the SAME symbol (K-1
    cleanup). `keep_id` is set to own the whole symbol-level Position (one owner → session
    avg == Position avg → no blended cost basis); `merge_id` is deleted and any OPUS rescue
    link repointed. The exchange is untouched — both sessions' fills already live in the one
    Position; this only fixes the session bookkeeping.
    """
    from app.models import Position
    from app.orchestrator.models import OpusPosition

    keep = _get_row(db, keep_id)
    merge = _get_row(db, merge_id)
    if keep_id == merge_id:
        raise ValueError("keep and merge are the same session")
    if keep.symbol != merge.symbol:
        raise ValueError(f"symbol mismatch: {keep.symbol} != {merge.symbol}")
    pos = db.query(Position).filter(Position.symbol == keep.symbol).one_or_none()
    if pos is None or pos.quantity <= 0 or pos.avg_entry_price <= 0:
        raise ValueError(f"no live Position for {keep.symbol} to consolidate")

    keep.total_filled_qty = pos.quantity
    keep.total_cost = pos.total_cost
    keep.avg_price = pos.avg_entry_price
    keep.isolated_fund = keep.isolated_fund + merge.isolated_fund
    keep.last_fill_at = datetime.utcnow()
    db.query(OpusPosition).filter(OpusPosition.kss_session_id == merge_id).update(
        {"kss_session_id": keep_id}
    )
    audit.log(db, "kss", "consolidate", entity=f"kss:{keep_id}", symbol=keep.symbol,
              merged=merge_id, qty=round(pos.quantity, 6), avg=round(pos.avg_entry_price, 8))
    db.delete(merge)
    db.commit()
    return {
        "message": f"Session {merge_id} merged into {keep_id}",
        "symbol": keep.symbol,
        "total_filled_qty": keep.total_filled_qty,
        "avg_price": keep.avg_price,
        "total_cost": keep.total_cost,
        "isolated_fund": keep.isolated_fund,
    }


def stop_session(db: Session, session_id: int, reason: str = "manual") -> dict:
    row = _get_row(db, session_id)
    if row.status != SESSION_ACTIVE:
        raise ValueError(f"Session {session_id} not active (status={row.status})")
    py = _to_pyramid(row)
    py.stop(reason)
    _save_state(row, py)
    db.commit()
    return {"message": f"Session {session_id} stopped", "reason": reason}


def _tp_clears_cost(db: Session, symbol: str, price: float) -> bool:
    """
    K-2 safety net: a take-profit may execute ONLY if `price` clears the TRUE aggregate
    cost basis of the coin's Position plus 2x the highest fee (costengine.min_profit_pct).
    Guards against any residual blended basis (legacy multi-session coins, manual orders)
    realizing a 'profit' that is actually a loss on the real book.
    """
    from app import costengine
    from app.models import Position

    pos = db.query(Position).filter(Position.symbol == symbol).one_or_none()
    if pos is None or pos.quantity <= 0 or pos.avg_entry_price <= 0:
        return True  # no aggregate basis to compare against → don't block
    floor = costengine.min_profit_pct() / 100.0  # 2 x binance_max_fee_pct
    return price >= pos.avg_entry_price * (1 + floor)


def manage_open_sessions(db: Session) -> list[int]:
    """
    Check every ACTIVE session against the live price and queue a TP sell when the
    take-profit threshold is reached. Used by the background scheduler. Returns the
    session ids that triggered TP. The TP sell still goes through the approval queue.

    K-2: a TP that would realize below the true aggregate cost basis (+2x fee) is DEFERRED
    (session stays ACTIVE) instead of selling at a loss.
    """
    from app.market import get_current_prices

    active = db.query(KssSession).filter(KssSession.status == SESSION_ACTIVE).all()
    if not active:
        return []
    prices = get_current_prices(list({s.symbol for s in active}))
    triggered: list[int] = []
    for row in active:
        price = prices.get(row.symbol)
        if not price:
            continue
        py = _to_pyramid(row)
        if py.total_filled_qty <= 0:
            continue
        res = py.check_tp(price)
        if res and not _tp_clears_cost(db, row.symbol, price):
            # K-2 defer: market hit the session TP but it would realize below true cost+fees.
            py.status = PyramidSessionStatus.ACTIVE
            audit.log(db, "scheduler", "tp_deferred", entity=f"kss:{row.id}",
                      symbol=row.symbol, price=price)
            res = None  # fall through to the stop-loss check below
        if res:
            _queue(db, res["order"])
            _save_state(row, py)
            audit.log(db, "scheduler", "tp_queued", entity=f"kss:{row.id}",
                      symbol=row.symbol, price=price)
            triggered.append(row.id)
        else:
            # check_stop also updates peak_price; always save so the high-water
            # mark is persisted even when neither exit triggers.
            res = py.check_stop(price)
            if (res and res.get("action") == "trailing_stop"
                    and not _tp_clears_cost(db, row.symbol, price)):
                # K-trail: a trailing stop must only LOCK PROFIT, never sell below the true
                # cost basis + fees. Defer — the hard stop-loss (sl_pct) still cuts genuine
                # losers; the position rides so DCA can pull avg down toward TP.
                audit.log(db, "scheduler", "trailing_deferred", entity=f"kss:{row.id}",
                          symbol=row.symbol, price=price)
                _save_state(row, py)  # persist the peak high-water mark
            elif res:
                _queue(db, res["order"])
                _save_state(row, py)
                audit.log(
                    db, "scheduler", "stop_queued", entity=f"kss:{row.id}",
                    symbol=row.symbol, price=price, kind=res["action"],
                )
                triggered.append(row.id)
            else:
                _save_state(row, py)
    db.commit()
    return triggered


def manage_orphan_positions(db: Session) -> list[str]:
    """
    TP/SL-manage HELD positions that no active KSS session or OPUS position covers — leftover
    quantity from sessions that already closed (the session sold its own qty but the
    symbol-level Position kept a remainder). Without this they ride forever with no exit
    (e.g. a coin sitting at +25% but never taking profit).

    Sells at market when unrealized ≥ scan_tp_pct (and clears cost+fee, K-2) or ≤ −sl_pct.
    """
    from app.config import settings
    from app.market import get_current_prices
    from app.models import APPROVED, PENDING, PendingOrder, Position
    from app.orchestrator.models import OPUS_RIDE, OPUS_WATCH, OpusPosition

    positions = db.query(Position).filter(Position.quantity > 0).all()
    if not positions:
        return []
    kss_syms = {s.symbol for s in db.query(KssSession).filter(KssSession.status == SESSION_ACTIVE)}
    opus_syms = {p.symbol for p in db.query(OpusPosition).filter(
        OpusPosition.state.in_((OPUS_WATCH, OPUS_RIDE)))}
    # A symbol with an exit (SELL) already queued/approved is NOT an orphan: its position is about
    # to be sold by that in-flight order. The classic case is a KSS TP — the session queues its
    # SELL and goes inactive in the SAME cycle, but the SELL has not filled yet, so the leftover
    # qty would be mistaken for an orphan and sold a SECOND time (phantom fill + double-counted
    # fee, and pre-guard double-realized P&L). Defer; a genuine leftover remainder is swept on a
    # later cycle once no SELL is in flight.
    pending_sell_syms = {
        sym for (sym,) in db.query(PendingOrder.symbol).filter(
            PendingOrder.status.in_((PENDING, APPROVED)), PendingOrder.side == "SELL"
        )
    }
    managed = kss_syms | opus_syms | pending_sell_syms

    orphans = [p for p in positions if p.symbol not in managed and p.avg_entry_price > 0]
    if not orphans:
        return []
    prices = get_current_prices([p.symbol for p in orphans])
    swept: list[str] = []
    for p in orphans:
        px = prices.get(p.symbol)
        if not px:
            continue
        upnl_pct = (px - p.avg_entry_price) / p.avg_entry_price * 100
        tag = None
        if upnl_pct >= settings.scan_tp_pct and _tp_clears_cost(db, p.symbol, px):
            tag = "tp"
        elif settings.sl_pct > 0 and upnl_pct <= -settings.sl_pct:
            tag = "sl"
        if not tag:
            continue
        orders.queue_order(db, symbol=p.symbol, side="SELL", quantity=p.quantity, price=0.0,
                           order_type="MARKET", source="kss", source_ref=f"orphan:{tag}",
                           strategy_name="Orphan", note=f"orphan {tag} @ {upnl_pct:.1f}%")
        audit.log(db, "scheduler", f"orphan_{tag}", entity=p.symbol, symbol=p.symbol,
                  price=px, upnl_pct=round(upnl_pct, 2))
        swept.append(p.symbol)
    db.commit()
    return swept


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
    # The geometric ladder is anchored to entry_price: price(n)=entry×(1-distance%)^n. Changing
    # distance_pct after waves have filled re-computes the NEXT rung at the new distance from
    # entry, so it jumps discontinuously away from the last fill (and can land below the SL).
    # Forbid the change once any wave is filled — start a fresh session for a different spacing.
    new_d = changes.get("distance_pct")
    if new_d is not None and abs(float(new_d) - row.distance_pct) > 1e-9:
        filled = (
            db.query(KssWave)
            .filter(KssWave.session_id == session_id, KssWave.status == WAVE_FILLED)
            .count()
        )
        if filled > 0:
            raise ValueError(
                "Không thể đổi distance_pct của session đã có sóng khớp — thang DCA neo theo "
                "entry sẽ bị đứt đoạn (sóng kế tiếp nhảy xa khỏi nấc vừa khớp). Dừng session và "
                "tạo session mới nếu cần khoảng cách khác."
            )
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
    if result and not _tp_clears_cost(db, row.symbol, current_price):
        # K-2 defer: would realize below true cost basis + fees.
        py.status = PyramidSessionStatus.ACTIVE
        _save_state(row, py)
        db.commit()
        payload["tp_triggered"] = False
        payload["tp_deferred"] = True
    elif result:
        _queue(db, result["order"])
        _save_state(row, py)
        db.commit()
        payload["tp_order_queued"] = True
    return payload


# --- reads --------------------------------------------------------------


def get_status(db: Session, session_id: int) -> dict:
    return _to_pyramid(_get_row(db, session_id)).get_status()


def ladder_status(db: Session, session_id: int) -> dict:
    """get_status() enriched with the prices the ladder view shows: SL, trailing, next wave."""
    py = _to_pyramid(_get_row(db, session_id))
    st = py.get_status()
    avg = st["avg_price"] or st["entry_price"] or 0.0
    st["sl_pct_eff"] = py.sl_pct
    st["trailing_pct_eff"] = py.trailing_pct
    st["sl_price"] = avg * (1 - py.sl_pct / 100.0) if avg > 0 and py.sl_pct > 0 else 0.0
    st["trailing_price"] = (
        py.peak_price * (1 - py.trailing_pct / 100.0)
        if py.peak_price > 0 and py.trailing_pct > 0
        else 0.0
    )
    pend = sorted(
        (w for w in st["waves"] if w.get("status") in ("pending", "sent")),
        key=lambda w: w.get("wave_num", 0),
    )
    st["next_wave_price"] = pend[0]["target_price"] if pend else 0.0
    return st


def list_sessions(
    db: Session, status: str | None = None, symbol: str | None = None, limit: int = 100
) -> list[dict]:
    q = db.query(KssSession)
    if status:
        q = q.filter(KssSession.status == status)
    if symbol:
        q = q.filter(KssSession.symbol == symbol)
    from sqlalchemy import case
    # ACTIVE sessions first, then most-recent — so live sessions are always at the top.
    active_first = case((KssSession.status == SESSION_ACTIVE, 0), else_=1)
    rows = q.order_by(active_first, KssSession.created_at.desc()).limit(limit).all()
    return [_to_pyramid(r).get_status() for r in rows]


def summary(db: Session) -> dict:
    from app import risk  # lazy: risk -> portfolio -> models; avoid an import cycle at load

    rows = db.query(KssSession).all()
    active = [r for r in rows if r.status == SESSION_ACTIVE]
    # reserved = full projected DCA-ladder cost a session set aside (isolated_fund);
    # deployed = cash actually spent on filled waves (total_cost). The two diverge a lot
    # because a session reserves its whole ladder up-front but fills it wave by wave.
    reserved = sum(r.isolated_fund for r in active)
    deployed = sum(r.total_cost for r in active)
    equity = risk.account_equity(db)
    return {
        "total_sessions": len(rows),
        "active_sessions": len(active),
        "total_isolated_fund": reserved,        # reserved across active sessions (planned ceiling)
        "active_used_fund": deployed,           # real cash deployed in active sessions
        "total_used_fund": sum(r.total_cost for r in rows),
        "equity": equity,
        # real free USDT a manual DCA+ can deploy right now (incl. the auto-backup reserve).
        "free_cash": _idle_deployable(db),
        # reservation as a share of equity, and how far it exceeds equity (over-commit).
        "reserved_pct_of_equity": (reserved / equity * 100.0) if equity > 0 else 0.0,
        "over_equity_pct": (max(0.0, reserved - equity) / equity * 100.0) if equity > 0 else 0.0,
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
