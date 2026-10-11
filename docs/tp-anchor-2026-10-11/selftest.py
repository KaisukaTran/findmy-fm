"""Self-test: the COPIED step_session (anchor 'avgcopy' = avg math) must reproduce the original
cps.step_session exactly (exit reason/day/waves identical, pnl within rounding) on real hourly data,
both bounds. Also checks entry/max TP-price algebra on a synthetic ladder."""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import per_session as ps  # noqa: E402
import tp_anchor_engine as eng  # noqa: E402
import scripts.capital_portfolio_study as cps  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402

db = sqlite3.connect(f"file:{ps.DB}?mode=ro", uri=True)
syms = [r[0] for r in db.execute("SELECT DISTINCT symbol FROM candles WHERE interval='1h' ORDER BY symbol")][::12]
n = bad = 0
for sym in syms:
    rows = db.execute("SELECT symbol, ts, high, low, close, quote_volume, open, volume FROM candles WHERE symbol=? AND interval='1h' ORDER BY ts", (sym,)).fetchall()
    cand = to_candles([tuple(r) for r in rows])
    for start in range(500, len(cand) - 1500, 700):
        for pess in (False, True):
            ca = ps.make_cfg("avg", pess)
            cb = ps.make_cfg("avgcopy", pess)
            a, _ = ps.run_session(cand, start, ca)
            b, _ = ps.run_session(cand, start, cb)
            n += 1
            ok = (a["reason"] == b["reason"] and a["waves_filled"] == b["waves_filled"]
                  and a["days"] == b["days"] and abs(a["pnl_usd"] - b["pnl_usd"]) < 1e-3 + 1e-4 * abs(a["pnl_usd"]))
            if not ok:
                bad += 1
                print("MISMATCH", sym, start, pess, a, b)
print(f"avg vs avgcopy: {n} sessions, {bad} mismatches")

# synthetic algebra: entry=100, 3 rungs filled at 100, 93, 86.49 weights 1,2,3
cfg = ps.make_cfg("entry", False)
st = cps.SessionState("X", [{"ts": 0, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1}] * 3, 0, cfg, wave0=28.0)
for i, (q, p) in enumerate(zip((1, 2, 3), (100, 93, 86.49))):
    st.fill_qty[i] = q * st.unit_qty
    st.fill_prices[i] = p
avg = cps._avg_price(st)
f = cps._tp_factor(cfg, 3)
print("avg", round(avg, 4), "factor", round(f, 5))
for an in ("avg", "entry", "max"):
    cfg.tp_anchor = an
    print(an, round(eng.tp_price(st, cfg, avg, 3), 4), "pnl%", eng.tp_pnl_pct(cfg, avg, eng.tp_price(st, cfg, avg, 3), 3))
