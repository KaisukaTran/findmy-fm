"""Tai mot lan, dung chung nhieu lan — va quan trong hon: KHONG de nhieu tien trinh
cung dap vao rate-limit cua Binance trong khi instance live dang quet.

Ghi ra mot file pickle {symbol: [Candle, ...]} de moi agent/phan tich sau doc tu dia.
"""
from __future__ import annotations
import json, pickle, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.data.providers import data_provider  # noqa: E402

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", required=True)
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--limit", type=int, default=6570)   # 3 nam nen 4h
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    syms = [r["base"] for r in json.loads(Path(a.universe).read_text(encoding="utf-8"))]
    p = data_provider(); out = {}; t0 = time.time()
    for i, s in enumerate(syms, 1):
        try:
            rows = p.get_ohlcv(s, timeframe=a.timeframe, limit=a.limit)
        except Exception as exc:
            print(f"  ! {s}: {exc}", file=sys.stderr); continue
        if rows and len(rows) >= a.limit * 0.9:
            out[s] = rows
        if i % 10 == 0:
            print(f"  {i}/{len(syms)}  ({time.time()-t0:.0f}s)", file=sys.stderr)
    Path(a.out).write_bytes(pickle.dumps(out))
    n = sum(len(v) for v in out.values())
    print(f"{len(out)} coin, {n:,} nen {a.timeframe} -> {a.out}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
