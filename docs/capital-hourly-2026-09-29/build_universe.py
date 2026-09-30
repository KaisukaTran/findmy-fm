"""Build the honest (non-survivorship-biased) 1h universe for 2024-01..2026-07.

WHY: `data/research/market.db`'s `1h` table held only the 150 symbols
`research_dataset.py klines --like-universe` picked by 2025+ median quote volume -- a
survivor-biased list (no delisted coin, nothing listed after that ranking was taken). The `1d`
table already has 2021-2026 for 641 symbols (`research_dataset.py klines --interval 1d`, no
`--like-universe`), so it is the honest reference for "which symbols existed": every USDT spot
symbol with ANY `1d` bar inside the study window, whether or not it survived to the end of it
or to today.

This script only PRINTS the plan and (with --run) launches the download; it does not run the
portfolio grid.

    python docs/capital-hourly-2026-09-29/build_universe.py            # dry run, prints the plan
    python docs/capital-hourly-2026-09-29/build_universe.py --run      # launches the download

Writes into `data/research/market.db` itself (the SAME file, same `candles`/`parts` tables the
150-symbol 1h set already lives in) rather than a separate `market_1h_full.db`: the loader
(`research_dataset.py klines`) tracks progress per (symbol, month) in that file's own `parts`
table, so downloading into the existing db means the 150 symbols already loaded are skipped
(resumable) instead of re-fetched, and every later script (`capital_hourly_grid.py`,
`scripts/liquidity_tier_study.load`) reads one file for both `1d` and `1h` without a merge step.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB = ROOT / "data/research/market.db"

WINDOW_START_TS = 1704067200000  # 2024-01-01T00:00:00Z
WINDOW_END_TS = 1785638400000    # 2026-08-01T00:00:00Z (i.e. through 2026-07-31)


def universe() -> tuple[list[str], list[str], list[str]]:
    """Returns (all_1d_symbols_in_window, already_have_1h, need_1h)."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT symbol FROM candles WHERE interval='1d' AND ts>=? AND ts<=?",
        (WINDOW_START_TS, WINDOW_END_TS),
    )
    have_1d = sorted(r[0] for r in cur.fetchall())
    cur.execute("SELECT DISTINCT symbol FROM candles WHERE interval='1h'")
    have_1h = {r[0] for r in cur.fetchall()}
    need = sorted(s for s in have_1d if s not in have_1h)
    conn.close()
    return have_1d, sorted(have_1h), need


def delisted_count(symbols: list[str]) -> int:
    """A symbol counts as delisted-before-window-end if its last 1d bar is more than 3 days
    before the window's end (mirrors capital_portfolio_study.py's `_close_delisted` trigger:
    the session's data simply stops)."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    n = 0
    for s in symbols:
        cur.execute(
            "SELECT MAX(ts) FROM candles WHERE interval='1d' AND symbol=? AND ts<=?",
            (s, WINDOW_END_TS),
        )
        last = cur.fetchone()[0]
        if last is not None and last < WINDOW_END_TS - 3 * 86_400_000:
            n += 1
    conn.close()
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true", help="actually launch the download")
    args = p.parse_args()

    have_1d, have_1h, need = universe()
    print(f"USDT spot symbols with a 1d bar in [2024-01-01, 2026-07-31]: {len(have_1d)}")
    print(f"already have 1h: {len(have_1h)}")
    print(f"need to download 1h for: {len(need)}")
    n_delisted = delisted_count(have_1d)
    print(f"of the {len(have_1d)}, delisted-before-window-end (proxy: last 1d bar "
          f">3 days before 2026-07-31): {n_delisted}")

    if not args.run:
        print("\n(dry run -- pass --run to launch the download)")
        return 0

    cmd = [
        sys.executable, str(ROOT / "scripts/research_dataset.py"),
        "--out", str(DB), "klines", "--interval", "1h",
        "--symbols", ",".join(need), "--start", "2024-01", "--end", "2026-07",
    ]
    print("running:", " ".join(cmd[:6]), f"... ({len(need)} symbols)")
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
