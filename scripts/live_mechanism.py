"""Backtest DUNG co che live — khong phai mot thang co dinh voi tp co dinh.

Live khac ban backtest truoc o HAI diem ban chat:
  1. tp_pct va distance_pct duoc AUTOTUNE tu ATR-14 NGAY cua CHINH coin do
     (`autotune.fit_levels`): tp = clamp(atr*tp_mult, 0.2, 15), dca = clamp(atr*0.5, 0.5, 10).
     Mot con so tp toan cuc cho ca vu tru la dung cai ma autotune duoc xay de sua.
  2. Co Ride&Trail (`kss/dynamic_exit.py` + `service._dynamic_exit`): trong vung lai nhung
     chua toi nguong arm thi KHONG co tran chot loi; qua nguong arm thi HUY THANG DCA va
     chuyen sang trailing-stop ratchet.

NHUNG tren live maker, `sync_resting_tp` (service.py:2115) giu mot lenh chot loi NAM SAN cho
moi phien ACTIVE va KHONG kiem `trail_active` — trong khi tp autotune (~2.7-4.9%) luon THAP
hon nguong arm (5%). Nghia la lenh chot co dinh khop TRUOC khi kip arm: **Ride&Trail chua
tung chay tren live**. Do la loi K-1. Nen o day do CA HAI:

  A_thuc_te : autotune + chot loi co dinh nam san  = live DANG chay hom nay
  B_thiet_ke: autotune + Ride&Trail, KHONG tran tp = live neu K-1 duoc sua

Chenh lech giua A va B chinh la GIA TRI cua viec sua K-1.
Moi mo phong dung thu tu BI QUAN (bat loi truoc, chot loi sau) va chi phi khu hoi that.
"""
from __future__ import annotations

import argparse
import math
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import costengine  # noqa: E402

MS_DAY = 86_400_000.0


def to_daily(bars):
    """Gop nen 4h thanh nen NGAY — ATR cua autotune tinh tren nen ngay.

    `end_ts` = moc cua nen 4h CUOI CUNG trong ngay do. Bat buoc phai co: mot nen ngay chi
    duoc dung khi NO DA DONG hoan toan truoc thoi diem vao lenh. Ban dau o day chi loc theo
    `ts` (moc MO ngay), nen mot nen ngay dang chay — chua ket thuc — van lot vao ATR, keo
    theo ca nhung gio SAU khi vao lenh. Do la nhin trom, va no dinh gan nhu MOI luot vi
    buoc lay mau 42 nen 4h = dung 7 ngay, luon roi vao cung mot gio trong ngay.
    Do duoc luc phat hien: ATR bi thoi phong 78,8% so luot, trung vi +0,68%, p90 +4,1%.
    """
    out, cur, day = [], None, None
    for b in bars:
        d = int(b["ts"] // MS_DAY)
        if d != day:
            if cur:
                out.append(cur)
            cur = {"ts": b["ts"], "end_ts": b["ts"], "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"]}
            day = d
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["end_ts"] = b["ts"]
    if cur:
        out.append(cur)
    return out


def atr_pct(daily, end_ts, n=14):
    """Ban sao autotune.atr_pct: trung binh TR/close*100 tren n nen ngay DA DONG truoc end_ts."""
    hist = [d for d in daily if d["end_ts"] < end_ts]
    if len(hist) < n + 1:
        return 0.0
    rs = []
    for prev, bar in zip(hist[-n - 1:-1], hist[-n:]):
        close = float(bar["close"]) or 1.0
        tr = max(bar["high"] - bar["low"],
                 abs(bar["high"] - prev["close"]),
                 abs(bar["low"] - prev["close"]))
        rs.append(tr / close * 100.0)
    return sum(rs) / len(rs) if rs else 0.0


def run_trial(bars, daily, start, *, mode, max_waves, sl_pct, deadline_days, cost_pct,
              notional, tp_mult=0.85, dca_mult=0.5, arm_pct=5.0, lock_pct=2.0,
              trail_atr_mult=1.0, trail_min_pct=3.0, tp_gap_pct=5.0, exit_fee_mult=3.0):
    entry = bars[start]["close"]
    entry_ts = bars[start]["ts"]
    atr = atr_pct(daily, entry_ts)
    if atr <= 0:
        return None  # khong du lich su -> autotune bo qua, global mac dinh moi ap dung
    tp_pct = min(max(atr * tp_mult, 0.2), 15.0)
    d_pct = min(max(atr * dca_mult, 0.5), 10.0)

    d = d_pct / 100.0
    targets = [entry * (1 - d) ** n for n in range(max_waves)]
    fills = [entry] + [targets[i] for i in range(1, max_waves)]
    w = [n + 1 for n in range(max_waves)]
    filled = 1
    unit = notional / entry

    def avg_of(k):
        return sum(fills[i] * w[i] for i in range(k)) / sum(w[i] for i in range(k))

    def cost_of(i):
        return w[i] * unit * fills[i]

    deployed = cost_of(0)
    acc = 0.0
    prev_ts = entry_ts
    armed = False
    peak = 0.0
    carried_sl = 0.0
    fee_floor_f = 1 + exit_fee_mult * cost_pct / 100.0

    def bar_len(j):
        return (bars[j + 1]["ts"] - bars[j]["ts"]) / MS_DAY if j + 1 < len(bars) else 0.0

    def done(kind, pnl, j):
        return dict(kind=kind, pnl=pnl, cap_days=acc + deployed * bar_len(j), cap=deployed,
                    atr=atr, tp_pct=tp_pct, d_pct=d_pct, waves=filled, armed=armed)

    def ratchet(pk, avg, prev):
        td = max(trail_atr_mult * atr, trail_min_pct)
        target = pk * (1 - td / 100.0)
        floor = max(avg * fee_floor_f, avg * (1 + lock_pct / 100.0))
        if target > avg and d > 0:
            k = max(math.floor(math.log(target / avg) / math.log(1 + d)), 0)
            grid = avg * (1 + d) ** k
        else:
            grid = avg
        return max(grid, floor, prev)

    for j in range(start + 1, len(bars)):
        bar = bars[j]
        days = (bar["ts"] - entry_ts) / MS_DAY
        acc += deployed * (bar["ts"] - prev_ts) / MS_DAY
        prev_ts = bar["ts"]
        avg = avg_of(filled)

        if armed:
            # kenh trailing tren sl/tp DA MANG SANG (dung thu tu cua live);
            # bi quan: kiem SL truoc TP trong cung mot nen
            tp_px = max(carried_sl * (1 + tp_gap_pct / 100.0), avg * fee_floor_f)
            if bar["low"] <= carried_sl:
                return done("trail_sl", (carried_sl - avg) / avg * 100 - cost_pct, j)
            if bar["high"] >= tp_px:
                return done("spike_tp", (tp_px - avg) / avg * 100 - cost_pct, j)
            peak = max(peak, bar["high"])
            carried_sl = ratchet(peak, avg, carried_sl)
            if days >= deadline_days:
                return done("deadline", (bar["close"] - avg) / avg * 100 - cost_pct, j)
            continue

        # --- chua arm ---
        if mode == "A":  # chot loi co dinh NAM SAN (live hom nay, vi K-1)
            if bar["high"] >= avg * (1 + tp_pct / 100.0):
                return done("tp", tp_pct - cost_pct, j)
        else:  # B: khong tran tp; trong vung lai thi kiem nguong arm
            if bar["high"] >= avg * (1 + arm_pct / 100.0):
                armed = True
                peak = bar["high"]
                carried_sl = ratchet(peak, avg, 0.0)
                continue  # arm roi thi huy thang DCA, khong lap rung nua

        while filled < max_waves and bar["low"] <= targets[filled]:
            fills[filled] = min(targets[filled], bar["open"])
            deployed += cost_of(filled)
            filled += 1
        avg = avg_of(filled)

        if sl_pct > 0 and bar["low"] <= avg * (1 - sl_pct / 100.0):
            return done("sl", -sl_pct - cost_pct, j)
        if days >= deadline_days:
            return done("deadline", (bar["close"] - avg) / avg * 100 - cost_pct, j)

    avg = avg_of(filled)
    return done("eod", (bars[-1]["close"] - avg) / avg * 100 - cost_pct, len(bars) - 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--notional", type=float, default=40.0)
    ap.add_argument("--step", type=int, default=42)
    ap.add_argument("--warmup", type=int, default=120)
    ap.add_argument("--tp-mult", type=float, default=0.85)
    ap.add_argument("--deadline", type=float, default=7.0)
    a = ap.parse_args()

    U = pickle.loads(Path(a.data).read_bytes())
    DAILY = {s: to_daily(b) for s, b in U.items()}
    cost = costengine.round_trip_cost_pct()
    print(f"{len(U)} coin | wave-0 ${a.notional:.0f} | chi phi {cost:.2f}% | "
          f"tp_mult {a.tp_mult} | dca_mult 0.5 | han {a.deadline:g}d | warmup {a.warmup} nen")
    print()
    CFG = [("3 song / SL 8  (hinh hoc live)", 3, 8.0),
           ("5 song / SL 20 (Kai)", 5, 20.0)]
    hdr = (f'{"cau hinh":<32}{"che do":>9}{"luot":>7}{"tong $":>10}{"$/luot":>9}'
           f'{"$/do-ngay":>12}{"thang%":>8}{"cat lo%":>9}{"het han%":>9}')
    print(hdr)
    print("-" * len(hdr))
    for name, waves, sl in CFG:
        for mode, label in (("A", "thuc te"), ("B", "thiet ke")):
            tot = 0.0
            days = 0.0
            n = 0
            kinds: dict[str, int] = {}
            for s, bars in U.items():
                dly = DAILY[s]
                for i in range(a.warmup, len(bars) - 60, a.step):
                    r = run_trial(bars, dly, i, mode=mode, max_waves=waves, sl_pct=sl,
                                  deadline_days=a.deadline, cost_pct=cost,
                                  notional=a.notional, tp_mult=a.tp_mult)
                    if r is None:
                        continue
                    tot += r["pnl"] / 100 * r["cap"]
                    days += r["cap_days"]
                    n += 1
                    kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            win = kinds.get("tp", 0) + kinds.get("spike_tp", 0) + kinds.get("trail_sl", 0)
            print(f'{name:<32}{label:>9}{n:>7}{tot:>+9.0f}${tot / n:>+8.3f}'
                  f'{tot / days:>12.6f}{win / n * 100:>7.1f}%'
                  f'{kinds.get("sl", 0) / n * 100:>8.1f}%'
                  f'{kinds.get("deadline", 0) / n * 100:>8.1f}%')
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
