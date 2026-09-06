"""
One-off maintenance: consolidate duplicate FET sessions #145 → #144 and extend the
DCA ladder (distance 3%, +10 waves). Does NOT queue any buy — activate later with the
"DCA+" button (or POST /api/kss/sessions/144/dca-next).

    python scripts/consolidate_fet.py            # dry-run: print the plan, no writes
    python scripts/consolidate_fet.py --apply    # perform it

Background: both sessions were OPUS rescues of the same coin (K-1 hole, now fixed). The
exchange position is untouched — both sessions' fills already live in one FET Position;
this only fixes the session bookkeeping so a single owner tracks the whole lot.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402
from app.kss import service  # noqa: E402
from app.models import KssSession, Position  # noqa: E402

KEEP_ID = 144
MERGE_ID = 145
NEW_DISTANCE_PCT = 3.0
EXTRA_WAVES = 10


def _fmt(row: KssSession) -> str:
    return (f"#{row.id} {row.symbol} status={row.status} waves={row.current_wave + 1}/"
            f"{row.max_waves} qty={row.total_filled_qty:.2f} avg={row.avg_price:.5f} "
            f"cost=${row.total_cost:.2f} fund=${row.isolated_fund:.2f} dist={row.distance_pct}%")


def _projected_ladder(entry: float, dist_pct: float, start_n: int, count: int) -> tuple[list, float]:
    """Geometric rungs (price only) for display; qty/cost come from the live session on apply."""
    rungs = []
    for n in range(start_n, start_n + count):
        price = round(entry * (1 - dist_pct / 100) ** n, 6)
        rungs.append((n, price))
    deepest = rungs[-1][1] if rungs else entry
    return rungs, deepest


def main(apply: bool) -> None:
    db = SessionLocal()
    try:
        keep = db.get(KssSession, KEEP_ID)
        merge = db.get(KssSession, MERGE_ID)
        if keep is None or merge is None:
            print(f"ERROR: session(s) not found: keep={keep}, merge={merge}")
            return
        pos = db.query(Position).filter(Position.symbol == keep.symbol).one_or_none()

        print("=== BEFORE ===")
        print(" keep :", _fmt(keep))
        print(" merge:", _fmt(merge))
        if pos:
            print(f" Position {pos.symbol}: qty={pos.quantity:.2f} avg={pos.avg_entry_price:.5f} "
                  f"cost=${pos.total_cost:.2f}")

        new_max = keep.current_wave + 1 + EXTRA_WAVES
        rungs, deepest = _projected_ladder(keep.entry_price, NEW_DISTANCE_PCT,
                                           keep.current_wave + 1, EXTRA_WAVES)
        drop_pct = (keep.entry_price - deepest) / keep.entry_price * 100
        print("\n=== PLAN ===")
        print(f" 1) consolidate #{MERGE_ID} -> #{KEEP_ID} (keeper owns whole Position; "
              f"fund {keep.isolated_fund:.0f}+{merge.isolated_fund:.0f}="
              f"{keep.isolated_fund + merge.isolated_fund:.0f})")
        print(f" 2) extend ladder: distance {keep.distance_pct}%->{NEW_DISTANCE_PCT}%, "
              f"max_waves {keep.max_waves}->{new_max}")
        print(f" 3) NO buy queued (manual activation later via DCA+)")
        print(f" new rungs (price only): " + ", ".join(f"w{n}@{p:.5f}" for n, p in rungs))
        print(f" deepest rung {deepest:.5f} ({drop_pct:.1f}% below entry)")

        if not apply:
            print("\nDRY-RUN — no changes written. Re-run with --apply to perform.")
            return

        out1 = service.consolidate_sessions(db, keep_id=KEEP_ID, merge_id=MERGE_ID)
        new_fund = out1["isolated_fund"]
        out2 = service.adjust_session(
            db, KEEP_ID, distance_pct=NEW_DISTANCE_PCT, max_waves=new_max, isolated_fund=new_fund
        )
        db.expire_all()
        keep = db.get(KssSession, KEEP_ID)
        print("\n=== AFTER ===")
        print(" consolidate:", out1)
        print(" adjust     :", out2)
        print(" keep :", _fmt(keep))
        print("\nDone. Activate DCA when ready: DCA+ button on the session, or "
              f"POST /api/kss/sessions/{KEEP_ID}/dca-next")
    finally:
        db.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv[1:])
