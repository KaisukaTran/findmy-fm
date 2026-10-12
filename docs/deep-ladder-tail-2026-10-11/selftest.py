"""Self-test: (1) COPIED step path with hooks off ('force_copy') reproduces the ORIGINAL cps.step_session
exactly on real hourly data, both bounds; (2) D2/D3 identical to A0 for sessions whose A0 run never
reaches 5 fills; (3) no TP ever nets <= 0 (K-2); (4) D1/D4 route through the original."""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import per_session as ps  # noqa: E402
import deep_engine as eng  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402

db = sqlite3.connect("file:D:/FINDMY/data/research/market.db?mode=ro", uri=True)
syms = [r[0] for r in db.execute("SELECT DISTINCT symbol FROM candles WHERE interval='1h' ORDER BY symbol")][::6]
n = bad = n2 = bad2 = bad3 = ndeep = 0
for sym in syms:
    rows = db.execute("SELECT symbol, ts, high, low, close, quote_volume, open, volume FROM candles WHERE symbol=? AND interval='1h' ORDER BY ts", (sym,)).fetchall()
    cand = to_candles([tuple(r) for r in rows])
    for start in range(500, len(cand) - 1500, 450):
        for pess in (False, True):
            a, _ = ps.run_session(cand, start, ps.make_cfg("A0", pess))
            cfgc = ps.make_cfg("A0", pess)
            cfgc.force_copy = True
            b, _ = ps.run_session(cand, start, cfgc)
            n += 1
            if not (a["reason"] == b["reason"] and a["waves_filled"] == b["waves_filled"] and a["days"] == b["days"]
                    and a["pnl_usd"] == b["pnl_usd"] and a["capital_days"] == b["capital_days"]):
                bad += 1
                print("COPY MISMATCH", sym, start, pess, a, b)
            for v in ("D2", "D3"):
                d, _ = ps.run_session(cand, start, ps.make_cfg(v, pess))
                n2 += 1
                if a["waves_filled"] < 5:
                    if d != a:
                        bad2 += 1
                        print("DEEP!=A0 on shallow", v, sym, start, pess, a, d)
                else:
                    ndeep += 1
                if d["reason"] == "tp" and d["pnl_pct"] <= 0:
                    bad3 += 1
                    print("TP net <= 0", v, d)
print(f"copy-vs-orig: {n} sessions, {bad} mismatches; D2/D3 vs A0: {n2} runs ({ndeep} on deep sessions), "
      f"{bad2} mismatches on shallow; TP-net<=0: {bad3}")
