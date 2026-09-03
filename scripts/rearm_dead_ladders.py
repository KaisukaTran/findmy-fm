"""
One-time re-arm migration for dead KSS ladders — P1 hardening (2026-09-01).

Background: four compounding bugs (deploy-headroom double-counting a just-filled wave,
pip_size re-pricing old sessions against a LATER global kss_first_wave_usd edit, the reserve
vs the spend disagreeing by float dust, and a silent insufficient-fund skip) left several live
sessions' DCA ladders starved of a fund they should still have had. Fixes 1-4 (this same
change set) stop it happening again; this script repairs the sessions it already happened to:

  1. Backfill ``KssSession.first_wave_usd`` where NULL (legacy rows, pre-Fix-2), from THIS
     session's OWN ``isolated_fund / ladder_factor(distance_pct, max_waves)`` — never a
     global, and never another session's numbers (each session's autotuned distance/max_waves
     differs). GUARDED (fix round B): the inversion (``est × ladder_factor == isolated_fund``)
     is trivially true by construction, so it proves nothing about whether ``est`` is actually
     a plausible first-wave USD value — a session opened with an explicit/manual
     ``isolated_fund`` or a legacy minQty-sized ladder would "invert" to a number that is not
     a first-wave USD at all. Sanity-bounded instead: skipped (report only, nothing written)
     when ``est <= 0``, or when the CURRENT ``settings.kss_first_wave_usd`` global is <= 0 (no
     plausible USD-ladder reference to check against — this also means "the global was 0 at
     this session's creation" skips ALL legacy rows, with a message), or when ``est`` falls
     outside ``[0.2x, 5x]`` of that global.
  2. Recompute the next rung's cost under the per-session first_wave_usd and report, per
     ACTIVE session: symbol, remaining_fund, old (global-priced) vs new (session-priced) rung
     cost, affordable yes/no. PENDING sessions are included in the backfill sweep (their
     future rungs are exposed to the exact same re-pricing bug) but have no rungs YET, so this
     step and the top-up below only run for ACTIVE ones.
  3. Where the shortfall (new rung cost - remaining_fund) is SMALL (<= $2.00), raise
     ``isolated_fund`` by that shortfall PLUS a pad (fix round B: real rungs snap qty to the
     exchange's stepSize — round-HALF, which can round UP — and price to its fixed precision,
     so an exact un-snapped shortfall left ~zero margin once the real snap ran; the pad covers
     that headroom, reported separately from the raw shortfall). A LARGER shortfall is only
     reported, never patched — patching it would mask real underfunding that deserves a human
     look, not a silent top-up.

This script NEVER queues an order — the app's own manage/fill-event cycle re-queues the rung
once it is affordable (the "Insufficient fund" warning already fires every cycle, which is
exactly the proof that the path re-evaluates and will pick it up).

OFFLINE ONLY: stop the app before pointing this at a live DB file — it writes directly to the
sqlite file with no coordination against a concurrently-running process.

Usage:
    python scripts/rearm_dead_ladders.py                    # dry-run report, data/live.db
    python scripts/rearm_dead_ladders.py --db data/live.db  # dry-run, explicit path
    python scripts/rearm_dead_ladders.py --apply             # write the backfill + small patches
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# The largest shortfall this script will silently close by bumping isolated_fund. Anything
# larger is reported only — see the module docstring's point 3.
_MAX_AUTO_PATCH_USD = 2.00

# Floor for the snapping pad added on top of a shortfall (fix round B item (c)) — a shortfall
# of a fraction of a cent still needs SOME real headroom against stepSize/precision rounding.
_MIN_PAD_USD = 0.01

# The backfill validity guard (fix round B item (b)): a legacy row's derived first_wave_usd is
# only trusted when it falls within this multiple of the CURRENT settings.kss_first_wave_usd
# global — outside this band it is more likely an explicit/manual isolated_fund or a legacy
# minQty-sized ladder than a genuine USD-ladder session.
_GUARD_MIN_MULT = 0.2
_GUARD_MAX_MULT = 5.0


def ladder_factor(distance_pct: float, max_waves: int) -> float:
    """Σ_{n=0}^{max_waves-1} (n+1)·(1−distance_pct/100)ⁿ — the pure-continuous (pre-step-
    snap) shape of a session's OWN ladder. isolated_fund = first_wave_usd × this factor
    whenever kss_first_wave_usd sizing was active at creation (qty(n)=(n+1)×first_wave_usd/
    entry, price(n)=entry×(1−d/100)ⁿ — entry cancels out of qty×price), so dividing it back
    out recovers first_wave_usd for a legacy (pre-Fix-2, NULL) row."""
    if max_waves < 1:
        return 0.0
    factor = 1 - distance_pct / 100.0
    return sum((n + 1) * (factor ** n) for n in range(max_waves))


def rung_cost(first_wave_usd: float, distance_pct: float, wave_num: int) -> float:
    """Raw (pre-step-snap) cost of ONE wave under first-wave-USD sizing:
    (wave_num+1) × first_wave_usd × (1−distance_pct/100)^wave_num — entry_price cancels out
    (qty=(wave_num+1)×first_wave_usd/entry, price=entry×(1−d/100)^wave_num). Deliberately the
    same un-snapped arithmetic ``projected_ladder_cost`` falls back to when step info is
    unavailable (app/market.py's ``_DEFAULT_INFO``) — this script runs with the app (and its
    exchange-info cache) stopped, so it has no reliable step size to snap against; the real
    app re-evaluates with proper snapping on its own next cycle regardless (see module
    docstring — this script never queues an order itself)."""
    factor = 1 - distance_pct / 100.0
    return (wave_num + 1) * first_wave_usd * (factor ** wave_num)


def _fmt(x: float) -> str:
    return f"${x:,.4f}"


def _process_session(row, settings, *, apply: bool) -> bool:
    """Report (and, with ``apply``, patch) one ACTIVE or PENDING session. Returns True if it
    was changed."""
    changed = False

    # 1. Backfill first_wave_usd where NULL, from THIS session's own numbers — GUARDED (fix
    #    round B item (b)): the inversion always "checks out" arithmetically, so it is sanity-
    #    bounded against the current global instead. A session skipped here is reported only;
    #    nothing is written, and it is treated as having no usable first_wave_usd below.
    usable_estimate = row.first_wave_usd
    if row.first_wave_usd is None:
        factor = ladder_factor(row.distance_pct, row.max_waves)
        est = row.isolated_fund / factor if factor > 0 else 0.0
        global_fw = settings.kss_first_wave_usd
        skip_reason = None
        if est <= 0:
            skip_reason = "estimate <= 0 — isolated_fund could not plausibly be USD-ladder-derived"
        elif global_fw <= 0:
            skip_reason = ("kss_first_wave_usd global is currently 0 — no plausible USD-ladder "
                            "value to sanity-check the estimate against")
        elif not (_GUARD_MIN_MULT * global_fw <= est <= _GUARD_MAX_MULT * global_fw):
            skip_reason = (
                f"estimate {_fmt(est)} is outside [{_GUARD_MIN_MULT}x, {_GUARD_MAX_MULT}x] of "
                f"the global kss_first_wave_usd ({_fmt(global_fw)}) — likely an explicit/manual "
                "isolated_fund or a minQty-sized ladder, not USD-ladder sizing"
            )

        if skip_reason:
            print(f"[{row.symbol} #{row.id}] first_wave_usd NULL -> SKIPPED "
                  f"(est={_fmt(est)}): {skip_reason}")
            usable_estimate = None
        else:
            print(f"[{row.symbol} #{row.id}] first_wave_usd NULL -> backfill {_fmt(est)} "
                  f"(isolated_fund={_fmt(row.isolated_fund)}, distance={row.distance_pct}%, "
                  f"max_waves={row.max_waves})")
            usable_estimate = est
            if apply:
                row.first_wave_usd = est
                changed = True
    else:
        print(f"[{row.symbol} #{row.id}] first_wave_usd already set: {_fmt(usable_estimate)}")

    # PENDING sessions have no rungs yet (fix round B item (d)) — the backfill above is
    # everything that applies to them; the rung-cost recompute/top-up below is ACTIVE-only.
    if row.status != "active":
        print(f"    status={row.status} — no rungs placed yet, skipping rung-cost recompute\n")
        return changed

    if usable_estimate is None:
        print("    first_wave_usd is unusable (backfill skipped above) — cannot recompute rung "
              "cost without it\n")
        return changed

    # 2. Recompute the next rung's cost, old (current global) vs new (this session's own value).
    next_wave_num = row.current_wave + 1
    if next_wave_num >= row.max_waves:
        print(f"    ladder already exhausted (current_wave={row.current_wave}, "
              f"max_waves={row.max_waves}) — nothing to re-arm\n")
        return changed

    remaining = max(row.isolated_fund - row.total_cost, 0.0)
    new_cost = rung_cost(usable_estimate, row.distance_pct, next_wave_num)
    old_cost = (
        rung_cost(settings.kss_first_wave_usd, row.distance_pct, next_wave_num)
        if settings.kss_first_wave_usd > 0 else None
    )
    old_str = _fmt(old_cost) if old_cost is not None else "n/a (legacy pip sizing, global is off)"
    affordable = new_cost <= remaining
    print(f"    wave {next_wave_num}: remaining={_fmt(remaining)} "
          f"old_rung(global)={old_str} new_rung(session)={_fmt(new_cost)} "
          f"affordable={'yes' if affordable else 'no'}")

    # 3. Small shortfall -> patch isolated_fund by the delta PLUS a snapping pad (fix round B
    #    item (c)). Larger -> report only.
    if not affordable:
        shortfall = new_cost - remaining
        if shortfall <= _MAX_AUTO_PATCH_USD:
            pad = max(_MIN_PAD_USD, new_cost * settings.kss_ladder_reserve_slack_pct / 100.0)
            topup = shortfall + pad
            verb = "raising" if apply else "WOULD raise"
            print(f"    -> shortfall {_fmt(shortfall)} <= {_fmt(_MAX_AUTO_PATCH_USD)}: "
                  f"{verb} isolated_fund by {_fmt(topup)} "
                  f"(shortfall {_fmt(shortfall)} + pad {_fmt(pad)} for real stepSize/precision "
                  "snapping)")
            if apply:
                row.isolated_fund += topup
                changed = True
        else:
            print(f"    -> shortfall {_fmt(shortfall)} > {_fmt(_MAX_AUTO_PATCH_USD)}: "
                  "reporting only (NOT patched — would mask real underfunding)")
    print()
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-arm KSS sessions starved by the pre-P1 headroom/pip_size/rounding bugs."
    )
    parser.add_argument("--db", default="data/live.db",
                        help="Path to the sqlite DB file (default: data/live.db)")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes (default: dry-run report only)")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = _ROOT / db_path
    if not db_path.exists():
        print(f"ERROR: DB file not found: {db_path}", file=sys.stderr)
        return 1

    # Point the app at THIS db file before importing anything that touches app.db/app.config
    # (both read DATABASE_URL at import time) — mirrors tests/app/conftest.py's pattern.
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.setdefault("REQUIRE_AUTH", "false")

    from app.config import settings
    from app.db import SessionLocal, init_db
    from app.models import SESSION_ACTIVE, SESSION_PENDING, KssSession

    # Fix round B item (a): a DB file that predates a later column migration (e.g.
    # first_wave_usd) would otherwise raise a raw sqlite3.OperationalError the moment this
    # queries KssSession — run the app's own additive-column migration first so a
    # not-yet-booted DB just gets caught up (idempotent; a no-op on an already-current DB).
    init_db()

    db = SessionLocal()
    try:
        # Fix round B item (d): PENDING sessions are included — their future rungs are just
        # as exposed to the global-re-pricing bug — but _process_session only runs the
        # rung-cost recompute/top-up for ACTIVE ones (a PENDING session has no rungs yet).
        sessions = (
            db.query(KssSession)
            .filter(KssSession.status.in_([SESSION_ACTIVE, SESSION_PENDING]))
            .order_by(KssSession.id)
            .all()
        )
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"{mode}: {len(sessions)} ACTIVE/PENDING session(s) in {db_path}\n")

        changed_count = sum(
            1 for row in sessions if _process_session(row, settings, apply=args.apply)
        )

        if args.apply:
            db.commit()
            print(f"Applied changes to {changed_count} session(s).")
        else:
            db.rollback()
            print("Dry-run only — no changes written. Re-run with --apply to write.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
