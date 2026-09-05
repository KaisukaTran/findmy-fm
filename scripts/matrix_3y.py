"""Ba năm, một ma trận: số rung x bước thang x mức cắt lỗ.

Câu hỏi của Kai: thang 3 rung + SL 8% có bị cắt lỗ hàng loạt trong một cú sập crypto không,
và đánh đổi giữa "DCA sâu / chịu SL lớn" với "mức sử dụng vốn" thực sự nằm ở đâu.

Không phát minh lại gì: dùng `app.evaluate.score_config` (đã gắn chi phí khứ hồi thật, đã
sửa lỗi nhìn trộm 2026-09-02, báo CẢ HAI biên intrabar) và `CcxtProvider.get_ohlcv` (đã có
phân trang qua trần 1000 nến của Binance). Chỉ đọc dữ liệu thị trường, KHÔNG chạm database.

Cột quan trọng nhất không phải win_rate mà là `stops` (tỉ lệ bị cắt lỗ) và `worst_mae`
(mức lỗ tạm tệ nhất) — đó là hai thứ trả lời câu "một cú sụp lớn có quét sạch sổ không".
"""
from __future__ import annotations

import argparse, json, sys, time
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data.providers import data_provider  # noqa: E402
from app.evaluate import score_config  # noqa: E402

DEFAULT_SYMBOLS = ("BTC,ETH,SOL,XRP,ADA,DOGE,LTC,LINK,AVAX,DOT,TRX,BCH,ATOM,NEAR,"
                   "FIL,ETC,UNI,ICP,APT,ARB,INJ,SEI,SUI,XLM")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--days", type=int, default=1095)          # 3 nam
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--waves", default="2,3,4,5")
    ap.add_argument("--distances", default="3,4,6,8")
    ap.add_argument("--sls", default="8,12,15,20")
    ap.add_argument("--tp", type=float, default=4.0)
    ap.add_argument("--deadline", type=float, default=7.0)
    ap.add_argument("--spacing-days", type=float, default=7.0)
    ap.add_argument("--skip-dates", default="",
                    help="Bo cac ngay YYYY-MM-DD khoi chuoi nen (vd 2025-10-10, ngay sap "
                         "thanh ly lon nhat lich su crypto) de tach kinh te che do BINH THUONG "
                         "khoi mot ngay duy nhat chi phoi toan bo trong so do-la.")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    skip = {d.strip() for d in a.skip_dates.split(",") if d.strip()}
    prov = data_provider()
    candles: dict[str, list] = {}
    t0 = time.time()
    for s in syms:
        try:
            rows = prov.get_ohlcv(s, timeframe=a.timeframe, limit=a.days)
        except Exception as exc:                                  # nen thieu != loi chay
            print(f"  ! {s}: {exc}", file=sys.stderr); continue
        if rows and skip:
            import datetime as _d
            before = len(rows)
            rows = [c for c in rows
                    if _d.datetime.utcfromtimestamp(c["ts"] / 1000).strftime("%Y-%m-%d") not in skip]
            if before != len(rows) and s == syms[0]:
                print(f"  (bo {before - len(rows)} nen theo --skip-dates)", file=sys.stderr)
        if rows:
            candles[s] = rows
            import datetime as _dt
            fmt = lambda ms: _dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
            print(f"  {s:<6} {len(rows):>5} nen  {fmt(rows[0]['ts'])} -> {fmt(rows[-1]['ts'])}",
                  file=sys.stderr)
    print(f"nap xong {len(candles)} coin trong {time.time()-t0:.1f}s\n", file=sys.stderr)
    if not candles:
        print("khong co du lieu", file=sys.stderr); return 1

    waves = [int(x) for x in a.waves.split(",")]
    dists = [float(x) for x in a.distances.split(",")]
    sls = [float(x) for x in a.sls.split(",")]

    rows_out = []
    hdr = (f'{"song":>4} {"buoc":>5} {"SL":>4} | {"lan":>5} {"thang%":>7} {"CAT LO%":>8} '
           f'{"het han%":>8} {"ky vong%":>9} {"MAE TB%":>8} {"MAE xau%":>9} '
           f'{"rung TB":>8} {"$/do-ngay":>10}')
    print(hdr); print("-" * len(hdr))
    for N, d, sl in product(waves, dists, sls):
        # Bien BI QUAN: trong mot nen, gia su cham SL truoc khi cham TP. Do la bien
        # dung cho cau hoi "co bi cat lo hang loat khong".
        sc = score_config(candles, distance_pct=d, tp_pct=a.tp, max_waves=N,
                          sl_pct=sl, deadline_days=a.deadline,
                          spacing_days=a.spacing_days, pessimistic_intrabar=True)
        if sc.trials == 0:
            continue
        stop_rate = sc.stops / sc.trials * 100
        flat_rate = sc.flats / sc.trials * 100
        print(f'{N:>4} {d:>4.0f}% {sl:>3.0f}% | {sc.trials:>5} {sc.win_rate:>6.1f}% '
              f'{stop_rate:>7.1f}% {flat_rate:>7.1f}% {sc.expectancy:>8.3f}% '
              f'{sc.avg_mae:>7.2f}% {sc.worst_mae:>8.2f}% {sc.avg_waves_filled:>7.2f} '
              f'{sc.profit_per_dollar_day:>10.6f}')
        rows_out.append({"waves": N, "distance_pct": d, "sl_pct": sl, "trials": sc.trials,
                         "win_rate_pct": sc.win_rate, "stop_rate_pct": stop_rate,
                         "flat_rate_pct": flat_rate, "expectancy": sc.expectancy,
                         "avg_mae": sc.avg_mae, "worst_mae": sc.worst_mae,
                         "avg_waves_filled": sc.avg_waves_filled,
                         "profit_per_dollar_day": sc.profit_per_dollar_day})
    if a.out:
        Path(a.out).write_text(json.dumps(rows_out, indent=1), encoding="utf-8")
        print(f"\n-> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
