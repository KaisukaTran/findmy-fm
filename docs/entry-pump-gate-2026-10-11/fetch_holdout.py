"""Fetch a HOLDOUT candle set (post-July 2026) from Binance public REST, slowly.

Writes `data/research/market_holdout.db` with the same `candles` schema as
`data/research/market.db` (that file is never opened for writing here). Reuses the symbol
filter (`is_study_symbol`) and the row layout of `scripts/research_dataset.py`:
REST kline = [open_ms, o, h, l, c, vol, close_ms, quote_vol, trades, ...] which is exactly the
archive CSV layout `parse_kline_csv` reads (index 7 = quote volume, 8 = trades).

Content
    1h : 2026-07-01 00:00Z -> last fully closed hour
    1d : 2025-07-01        -> last fully closed UTC day
    symbols: every symbol with 1h in market.db  +  top-150 USDT spot by 24h quote volume that
             market.db lacks (new listings).

Rate rules (public GET /api/v3/klines only, one request at a time)
    * >= DELAY s between requests, 60 s pause after every 50 requests
    * X-MBX-USED-WEIGHT-1M > 1200 -> sleep to next minute boundary + 5 s
    * 429 -> sleep Retry-After (120 s default), then DOUBLE the delay for the rest of the run
    * 418 -> STOP the run immediately (exit code 3)
    * resumable: per (symbol, interval) continues after the last stored ts

    python docs/entry-pump-gate-2026-10-11/fetch_holdout.py            # full run (resumes)
    python docs/entry-pump-gate-2026-10-11/fetch_holdout.py --symbols BTCUSDT,ETHUSDT --db X.db
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DB = ROOT / "data/research/market.db"
DEFAULT_OUT = ROOT / "data/research/market_holdout.db"
DEFAULT_LOG = Path(__file__).resolve().parent / "fetch_holdout.log"
API = "https://api.binance.com"

H_MS = 3_600_000
D_MS = 86_400_000
START = {
    "1h": int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000),
    "1d": int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000),
}
STEP = {"1h": H_MS, "1d": D_MS}

BASE_DELAY = 1.5
BATCH_EVERY = 50
BATCH_PAUSE = 60
WEIGHT_CEIL = 1200
TOP_N = 150


class Stop(Exception):
    """418 / unrecoverable: stop the whole run."""


def _load_rd():
    spec = importlib.util.spec_from_file_location("research_dataset", ROOT / "scripts/research_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # stdlib-only module, no side effects on import
    return mod


RD = _load_rd()

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL, trades INTEGER,
  PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_candles_ts ON candles(interval, ts);
CREATE TABLE IF NOT EXISTS fetch_status (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, status TEXT NOT NULL, note TEXT, at TEXT,
  PRIMARY KEY (symbol, interval)
) WITHOUT ROWID;
"""


class Client:
    def __init__(self, log_path: Path):
        self.delay = BASE_DELAY
        self.n_req = 0
        self.max_weight = 0
        self.n429 = 0
        self.last_t = 0.0
        self.last_used = 0
        self.logf = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(self, msg: str) -> None:
        line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}"
        print(line, flush=True)
        self.logf.write(line + "\n")

    def _sleep(self, secs: float, why: str) -> None:
        self.log(f"SLEEP {secs:.1f}s ({why})")
        time.sleep(secs)

    def get(self, path: str, params: dict | None = None):
        """Rate-limited GET. Returns parsed JSON; raises urllib.error.HTTPError for 4xx other
        than 429/418 (caller decides), Stop on 418."""
        qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        url = f"{API}{path}" + (f"?{qs}" if qs else "")
        attempts = 0
        while True:
            wait = self.last_t + self.delay - time.time()
            if wait > 0:
                time.sleep(wait)
            if self.n_req and self.n_req % BATCH_EVERY == 0:
                self._sleep(BATCH_PAUSE, f"batch pause after {self.n_req} requests")
            self.n_req += 1
            self.last_t = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": "findmy-research-holdout/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read()
                    used = int(r.headers.get("X-MBX-USED-WEIGHT-1M") or 0)
                self.max_weight = max(self.max_weight, used)
                self.last_used = used
                if used > WEIGHT_CEIL:
                    now = time.time()
                    self._sleep(60 - (now % 60) + 5, f"used-weight {used} > {WEIGHT_CEIL}")
                self.last_t = time.time()
                return json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code == 418:
                    self.log(f"HTTP 418 IP BAN on {path} -- STOPPING RUN")
                    raise Stop("418")
                if e.code == 429:
                    self.n429 += 1
                    ra = e.headers.get("Retry-After")
                    secs = float(ra) if ra and ra.isdigit() else 120.0
                    self.delay *= 2
                    self.log(f"HTTP 429 -> delay now {self.delay:.1f}s")
                    self._sleep(secs, "429 Retry-After")
                    attempts += 1
                    if attempts >= 3:
                        raise Stop("repeated 429")
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                attempts += 1
                if attempts >= 3:
                    raise
                self._sleep(10 * attempts, f"network error {e!r}")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def build_universe(cl: Client) -> tuple[list[str], list[str], dict]:
    src = sqlite3.connect(f"file:{SRC_DB.as_posix()}?mode=ro", uri=True)
    have = sorted(r[0] for r in src.execute("SELECT DISTINCT symbol FROM candles WHERE interval='1h'"))
    src.close()
    info = cl.get("/api/v3/exchangeInfo", {"permissions": "SPOT"})
    status = {s["symbol"]: s["status"] for s in info["symbols"] if s.get("quoteAsset") == "USDT"}
    tick = cl.get("/api/v3/ticker/24hr")
    ranked = sorted(
        (t for t in tick if t["symbol"] in status and status[t["symbol"]] == "TRADING"
         and t["symbol"].isascii() and RD.is_study_symbol(t["symbol"])),
        key=lambda t: float(t["quoteVolume"]), reverse=True)[:TOP_N]
    top = [t["symbol"] for t in ranked]
    new = [s for s in top if s not in set(have)]
    return have, new, status


def last_ts(db, sym, iv):
    r = db.execute("SELECT MAX(ts) FROM candles WHERE symbol=? AND interval=?", (sym, iv)).fetchone()
    return r[0]


def fetch_one(cl: Client, db, sym: str, iv: str, end_ms: int) -> tuple[str, int]:
    """Fetch [START, end_ms] for sym/iv, resuming. Returns (status, rows_added)."""
    step = STEP[iv]
    done = db.execute("SELECT status FROM fetch_status WHERE symbol=? AND interval=?", (sym, iv)).fetchone()
    if done and done[0] in ("empty", "invalid"):
        return done[0], 0
    lt = last_ts(db, sym, iv)
    cursor = START[iv] if lt is None else lt + step
    added = 0
    while cursor <= end_ms - step + 1:
        try:
            data = cl.get("/api/v3/klines", {"symbol": sym, "interval": iv, "startTime": cursor,
                                             "endTime": end_ms, "limit": 1000})
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:120]
            except Exception:
                pass
            db.execute("INSERT OR REPLACE INTO fetch_status VALUES (?,?,?,?,?)",
                       (sym, iv, "invalid", f"HTTP {e.code} {body}", datetime.now(timezone.utc).isoformat()))
            db.commit()
            return "invalid", added
        if not data:
            break
        rows = [(sym, iv, int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                 float(k[5]), float(k[7]), int(k[8])) for k in data if int(k[6]) <= end_ms]
        db.executemany("INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
        added += len(rows)
        cl.log(f"{sym} {iv} +{len(rows)} (to {datetime.fromtimestamp(data[-1][0]/1000, timezone.utc):%Y-%m-%d %H:%M}) "
               f"used-weight={cl.last_used}")
        nxt = int(data[-1][0]) + step
        if nxt <= cursor or len(data) < 1000:
            break
        cursor = nxt
    if added == 0 and lt is None:
        db.execute("INSERT OR REPLACE INTO fetch_status VALUES (?,?,?,?,?)",
                   (sym, iv, "empty", "no bars in range", datetime.now(timezone.utc).isoformat()))
        db.commit()
        return "empty", 0
    return "ok", added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_OUT))
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--symbols", help="comma list (dry run); default = full universe")
    args = ap.parse_args()
    if Path(args.db).resolve() == SRC_DB.resolve():
        raise SystemExit("refusing to write into the research market.db")

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time.time()
    cl = Client(Path(args.log))
    db = connect(Path(args.db))
    now = int(time.time() * 1000)
    end_h = now - now % H_MS - 1       # last ms of the last fully closed hour
    end_d = now - now % D_MS - 1       # last ms of the last fully closed UTC day
    cl.log(f"START db={args.db} end_h={end_h} end_d={end_d} delay={cl.delay}")
    rc = 0
    try:
        if args.symbols:
            syms = args.symbols.split(",")
            cl.log(f"explicit symbols: {syms}")
        else:
            have, new, status = build_universe(cl)
            syms = have + new
            cl.log(f"universe: {len(have)} from market.db 1h + {len(new)} new in top-{TOP_N}: {new}")
            gone = [s for s in have if s not in status]
            cl.log(f"{len(gone)} market.db symbols absent from exchangeInfo (probing 1d anyway)")
        total = {"1h": 0, "1d": 0}
        for i, sym in enumerate(syms, 1):
            st_d, nd = fetch_one(cl, db, sym, "1d", end_d)
            total["1d"] += nd
            st_h, nh = "skip", 0
            lastd = last_ts(db, sym, "1d")
            if st_d == "ok" and lastd is not None and lastd >= START["1h"]:
                st_h, nh = fetch_one(cl, db, sym, "1h", end_h)
                total["1h"] += nh
            elif st_d == "ok":
                db.execute("INSERT OR REPLACE INTO fetch_status VALUES (?,?,?,?,?)",
                           (sym, "1h", "empty", "1d ends before 2026-07-01 (delisted)", datetime.now(timezone.utc).isoformat()))
                db.commit()
            cl.log(f"[{i}/{len(syms)}] {sym}: 1d={st_d}(+{nd}) 1h={st_h}(+{nh}) reqs={cl.n_req} maxW={cl.max_weight}")
    except Stop as e:
        cl.log(f"STOPPED: {e}")
        rc = 3
    finally:
        db.commit()
        cl.log(f"END rc={rc} reqs={cl.n_req} max_weight={cl.max_weight} n429={cl.n429} "
               f"elapsed={(time.time()-t0)/60:.1f}min")
    return rc


if __name__ == "__main__":
    sys.exit(main())
