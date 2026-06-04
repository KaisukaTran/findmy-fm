"""
OPUS 3-hour watch state machine (O-4, docs §5).

Each tick, for every OPUS-managed position:
  - WATCH: at/after 3h, evaluate ONCE (confirmed rule: uPnL ≥ 0 at the 3h mark).
      winner → RIDE  (Opus keeps discretion; may bypass KSS — still under a hard ride-SL)
      loser  → RESCUE (hand the held lot to a standard KSS session; KSS rules take over)
  - RIDE: enforce the ride hard stop-loss so a reversing winner can't run unbounded.

Frozen pyramid math is untouched; rescue reuses kss.service.adopt_position_into_kss.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app import audit, market
from app.config import settings
from app.orchestrator import service
from app.orchestrator.models import OPUS_RESCUE, OPUS_RIDE, OPUS_WATCH

log = logging.getLogger(__name__)

_WATCH_HOURS = 3.0


def _age_hours(opened_at: datetime | None, now: datetime) -> float:
    return (now - (opened_at or now)).total_seconds() / 3600.0


def run(db: Session) -> dict:
    """Advance watch/ride state for all managed positions. Returns a small summary."""
    from app.kss import service as kss_service
    from app.orchestrator import policy

    positions = service.managed_positions(db)
    if not positions:
        return {"rides": 0, "rescues": 0, "ride_stops": 0}

    prices = market.get_current_prices(sorted({p.symbol for p in positions}))
    now = datetime.utcnow()
    rides = rescues = ride_stops = 0

    for pos in positions:
        price = prices.get(pos.symbol)
        if not price:
            continue
        avg = pos.avg_price or pos.entry_price or 0.0

        if pos.state == OPUS_WATCH and _age_hours(pos.opened_at, now) >= _WATCH_HOURS:
            upnl = (price - avg) * (pos.qty or 0.0)
            pos.evaluated_at = now
            if upnl >= 0:  # winner at the 3h mark → ride
                pos.state = OPUS_RIDE
                rides += 1
                audit.log(db, "opus", "ride", entity=f"opos:{pos.id}", symbol=pos.symbol,
                          upnl=round(upnl, 4))
            else:  # loser → hand off to KSS discipline
                session = kss_service.adopt_position_into_kss(
                    db, pos.symbol, pos.qty or 0.0, avg, price, note=f"opus-rescue:{pos.id}"
                )
                pos.state = OPUS_RESCUE
                pos.kss_session_id = session.id
                rescues += 1
                audit.log(db, "opus", "rescue", entity=f"opos:{pos.id}", symbol=pos.symbol,
                          kss=session.id, upnl=round(upnl, 4))

        elif pos.state == OPUS_RIDE and settings.opus_ride_hard_sl_pct > 0:
            stop = avg * (1.0 - settings.opus_ride_hard_sl_pct / 100.0)
            if price <= stop:
                try:
                    realized = policy.force_close(db, pos, "ride hard-stop")
                    if realized is not None:
                        ride_stops += 1
                        audit.log(db, "opus", "ride_stop", entity=f"opos:{pos.id}",
                                  symbol=pos.symbol, price=price, stop=round(stop, 6))
                except Exception as exc:  # e.g. breaker frozen — retry next tick
                    log.warning("ride hard-stop deferred (%s): %s", pos.symbol, type(exc).__name__)

    db.commit()
    return {"rides": rides, "rescues": rescues, "ride_stops": ride_stops}
