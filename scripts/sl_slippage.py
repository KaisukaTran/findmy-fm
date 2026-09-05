"""Cat lo trong `simulate_kss` khop HOAN HAO tai dung gia stop — bat ky nen thung sau toi dau.

`app/backtest.py:232` tra ve dung `-sl_pct - cost_pct` du nen co xuyen qua nguong bao nhieu,
trong khi chieu MUA lai CO mo phong gap (`_fill_price = min(target, bar_open)`, :59-71).
Su bat doi xung do co loi cho cau hinh SL RONG: mot cu thung du manh de xuyen SL 20% thi du
doi hon han cu xuyen SL 8%, nen do truot thuc te gan nhu chac chan TANG theo do rong SL —
dung cho ma cau hinh "tot nhat" dang duoc cho diem mien phi.
Chinh production da ghi mot lan truot that: service.py:1869 "-17.3% vs a -15% floor".

Ba mo hinh khop lenh dung:
  perfect : y het simulate_kss (alpha=0)  -> DUNG DE TU KIEM, phai trung khop tuyet doi
  gap     : khop tai min(gia_stop, gia_mo_nen) — DOI XUNG voi cach chieu mua duoc mo phong
  alpha   : khop tai gia_stop - alpha*(gia_stop - day_nen), alpha in [0,1]

KHONG sua app/. Day la ban phan chieu doc lap, va tinh dung dan cua no duoc chung minh
bang phep tu kiem alpha=0 chu khong bang loi noi.
"""
from __future__ import annotations
import argparse, pickle, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.backtest import _fill_price, _targets, simulate_kss, _MS_PER_DAY  # noqa: E402
from app import costengine  # noqa: E402


def sim(candles, start, distance_pct, max_waves, tp_pct, deadline_days, sl_pct,
        cost_pct, wave0_notional_usd, slip_model="perfect", alpha=0.0):
    """Ban phan chieu cua simulate_kss(pessimistic_intrabar=True) + mo hinh khop lenh stop."""
    entry = candles[start]["close"]; entry_ts = candles[start]["ts"]
    targets = _targets(entry, distance_pct, max_waves)
    weights = [n + 1 for n in range(max_waves)]
    fill_prices = [entry] + [targets[i] for i in range(1, max_waves)]
    filled = 1
    tp_f = 1 + tp_pct / 100; sl_f = 1 - sl_pct / 100
    unit_qty = wave0_notional_usd / entry if entry > 0 else 0.0
    avg_of = lambda k: (sum(fill_prices[i] * weights[i] for i in range(k))
                        / sum(weights[i] for i in range(k))) if k else entry
    wave_cost = lambda i: weights[i] * unit_qty * fill_prices[i]
    def bar_len(idx):
        if idx + 1 < len(candles): return (candles[idx+1]["ts"] - candles[idx]["ts"]) / _MS_PER_DAY
        if idx > 0: return (candles[idx]["ts"] - candles[idx-1]["ts"]) / _MS_PER_DAY
        return 0.0
    acc = 0.0; deployed = wave_cost(0); prev_ts = entry_ts
    close_cap = lambda idx: acc + deployed * bar_len(idx)

    for j in range(start + 1, len(candles)):
        bar = candles[j]; days = (bar["ts"] - entry_ts) / _MS_PER_DAY
        bar_open = bar.get("open", bar["close"])
        acc += deployed * (bar["ts"] - prev_ts) / _MS_PER_DAY; prev_ts = bar["ts"]

        pre_avg = avg_of(filled)                      # pessimistic: TP truoc, gia TB TRUOC khi lap
        if pre_avg > 0 and bar["high"] >= pre_avg * tp_f:
            return dict(kind="tp", pnl=tp_pct - cost_pct, cap_days=close_cap(j), cap=deployed)

        while filled < max_waves and bar["low"] <= targets[filled]:
            fill_prices[filled] = _fill_price(targets[filled], bar_open)
            deployed += wave_cost(filled); filled += 1
        avg = avg_of(filled)

        if sl_pct > 0 and bar["low"] <= avg * sl_f:
            stop_px = avg * sl_f
            if slip_model == "perfect":
                pnl = -sl_pct - cost_pct
            else:
                px = min(stop_px, bar_open) if slip_model == "gap" \
                     else max(bar["low"], stop_px - alpha * (stop_px - bar["low"]))
                pnl = (px - avg) / avg * 100 - cost_pct
            return dict(kind="sl", pnl=pnl, cap_days=close_cap(j), cap=deployed)

        if days >= deadline_days:
            return dict(kind="dl", pnl=(bar["close"] - avg) / avg * 100 - cost_pct,
                        cap_days=close_cap(j), cap=deployed)

    avg = avg_of(filled)
    return dict(kind="eod", pnl=(candles[-1]["close"] - avg) / avg * 100 - cost_pct,
                cap_days=close_cap(len(candles) - 1), cap=deployed)


def selfcheck(U, cfg, cost, notional):
    """alpha=0 PHAI trung khop simulate_kss tuyet doi — neu khong, ban phan chieu nay vo gia tri."""
    bad = n = 0
    for s, rows in list(U.items())[:12]:
        for i in range(0, len(rows) - 60, 300):
            a = simulate_kss(rows, i, cost_pct=cost, pessimistic_intrabar=True,
                             wave0_notional_usd=notional, **cfg)
            b = sim(rows, i, cost_pct=cost, wave0_notional_usd=notional,
                    slip_model="perfect", **cfg)
            n += 1
            ka = "tp" if a.tp_hit else ("sl" if a.stopped else ("dl" if a.hit_deadline else "eod"))
            if ka != b["kind"] or abs(a.pnl_pct - round(b["pnl"], 4)) > 1e-4 \
               or abs(a.exit_capital - round(b["cap"], 6)) > 1e-4:
                bad += 1
                if bad <= 3:
                    print(f"    LECH {s}@{i}: goc={ka}/{a.pnl_pct}/{a.exit_capital} "
                          f"moi={b['kind']}/{round(b['pnl'],4)}/{round(b['cap'],6)}")
    print(f"  TU KIEM alpha=0 vs simulate_kss: {n - bad}/{n} trung khop"
          f"{'  >> BAN PHAN CHIEU HOP LE' if bad == 0 else '  >> KHONG DUNG DUOC'}")
    return bad == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--notional", type=float, default=40.0)
    ap.add_argument("--step", type=int, default=42)
    a = ap.parse_args()
    U = pickle.loads(Path(a.data).read_bytes())
    cost = costengine.round_trip_cost_pct()
    CFGS = {
        "nay:  3 song/3.4%/SL8":  dict(distance_pct=3.4, max_waves=3, tp_pct=3.0, sl_pct=8.0,  deadline_days=7.0),
        "     4 song/8%/SL15":    dict(distance_pct=8.0, max_waves=4, tp_pct=3.0, sl_pct=15.0, deadline_days=7.0),
        "KAI: 5 song/8%/SL20":    dict(distance_pct=8.0, max_waves=5, tp_pct=3.0, sl_pct=20.0, deadline_days=7.0),
    }
    print(f"{len(U)} coin | wave-0 ${a.notional:.0f} | chi phi khu hoi {cost:.2f}%\n")
    if not selfcheck(U, list(CFGS.values())[0], cost, a.notional):
        return 1
    print()
    models = [("perfect", 0.0), ("gap", 0.0), ("alpha", 0.25), ("alpha", 0.5), ("alpha", 1.0)]
    hdr = f'{"cau hinh":<24}' + "".join(f'{(m if m!="alpha" else f"a={al:g}"):>12}' for m, al in models)
    print(hdr); print("-" * len(hdr))
    for name, cfg in CFGS.items():
        cells = []
        for m, al in models:
            tot = 0.0
            for rows in U.values():
                for i in range(0, len(rows) - 60, a.step):
                    r = sim(rows, i, cost_pct=cost, wave0_notional_usd=a.notional,
                            slip_model=m, alpha=al, **cfg)
                    tot += r["pnl"] / 100 * r["cap"]
            cells.append(f"{tot:>+11.0f}$")
        print(f"{name:<24}" + "".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
