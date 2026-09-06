"""Build the offline research dataset from Binance's public data archive.

WHY THIS EXISTS
    Every selection question this project has tried to answer died of sample size. With 6
    concurrent slots and ~0.55 average pairwise correlation the live book yields ~1.6
    independent bets at a time, so a signal worth having (IC 0.05) needs years of trading
    before its confidence interval clears zero. The 2026-08-31 gate measurement and the
    2026-09-06 ranking measurement both returned "CI includes zero" for exactly that reason,
    not because every signal tested was worthless.

    `data.binance.vision` removes the constraint: spot klines back to 2017-08, futures
    open-interest / long-short / taker-flow metrics back to 2020-09-01, funding back to
    2020-01 — free, no API weight, and **including delisted pairs**. A live probe finds 723
    USDT spot pairs in the archive against ~150 in the live universe, so a study built from
    today's tradable list would silently drop ~80% of the history and select on survival.

WHAT IT DOES
    Downloads, verifies and normalises those archives into one SQLite file that the study
    scripts read. Resumable: an interrupted run re-uses what is already stored.

    python scripts/research_dataset.py symbols
    python scripts/research_dataset.py klines --start 2021-01 --end 2026-08 [--interval 1d]
    python scripts/research_dataset.py metrics --start 2024-01 --end 2026-08 --top 100

TRAPS HANDLED HERE (each cost someone a day somewhere)
    * Spot archive timestamps switched from MILLISECONDS to MICROSECONDS on 2025-01-01.
      Joining the two eras unnormalised misaligns every date by a factor of 1000.
    * Newer archive files carry a CSV HEADER row; older ones do not.
    * Every zip has a sibling .CHECKSUM, and Binance reissues files silently after fixing
      data. We verify on download and store the hash.
    * Leveraged tokens (…UPUSDT / …DOWNUSDT / …BULLUSDT / …BEARUSDT) are not coins; they
      decay by construction and would pollute any cross-sectional study.
    * A missing month is normal (the pair had not listed yet) — 404 is data, not an error.

Stdlib only, so it runs in the app venv with no new dependency.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import io
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://data.binance.vision"
S3_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DEFAULT_DB = Path("data/research/market.db")

# Binance's WAF throttles bulk pulls to SSL errors if hit hard; a handful of workers is the
# documented-safe shape. This is a background job, not a latency-sensitive path.
WORKERS = 6
RETRIES = 3

# Leveraged tokens and wrapped/stable quote-side pairs are not coins for a cross-sectional study.
# A leveraged token is always <COIN><SUFFIX>, so the part before the suffix must itself look
# like a ticker. Without that length floor the rule eats real coins: JUP (Jupiter) ends in "UP"
# with a one-letter remainder, and a study that silently drops Jupiter is a study with a hole.
_LEVERAGED = re.compile(r"^(?P<coin>.{3,})(UP|DOWN|BULL|BEAR)$")
_STABLE_BASES = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDS", "SUSD", "EUR", "GBP",
    "AUD", "TRY", "BRL", "RUB", "USDT", "UST", "USTC", "AEUR", "USD1", "XUSD",
}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/app/test_research_dataset.py)
# ---------------------------------------------------------------------------

def normalize_ts(raw: str | int) -> int:
    """Archive timestamp -> milliseconds since epoch.

    Spot files switched to microseconds on 2025-01-01 while older files stay in milliseconds,
    and both appear in one symbol's history. Detect by magnitude rather than by filename date:
    a millisecond stamp for any plausible date is 13 digits, a microsecond stamp is 16.
    """
    v = int(raw)
    if v >= 1_000_000_000_000_000:      # >= ~2001 in microseconds
        return v // 1000
    return v


def is_study_symbol(symbol: str, quote: str = "USDT") -> bool:
    """True for a spot pair worth putting in a cross-sectional study."""
    if not symbol.endswith(quote):
        return False
    base = symbol[: -len(quote)]
    if not base or _LEVERAGED.match(base):
        return False
    return base not in _STABLE_BASES


def month_range(start: str, end: str) -> list[str]:
    """Inclusive list of 'YYYY-MM' strings."""
    y, m = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def parse_kline_csv(text: str) -> list[tuple]:
    """CSV body -> rows of (open_ms, open, high, low, close, volume, quote_volume, trades).

    Skips the header row that newer archive files carry, and any row that is not numeric —
    a corrupt line must not take the whole month down.
    """
    rows: list[tuple] = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 9:
            continue
        try:
            ts = normalize_ts(parts[0])
            rows.append((ts, float(parts[1]), float(parts[2]), float(parts[3]),
                         float(parts[4]), float(parts[5]), float(parts[7]), int(float(parts[8]))))
        except (ValueError, TypeError):
            continue                    # header line, or a damaged row
    return rows


def parse_metrics_csv(text: str) -> list[tuple]:
    """Futures metrics CSV -> (ts_ms, open_interest, oi_value, top_acct_ratio,
    top_pos_ratio, global_acct_ratio, taker_buy_sell_ratio)."""
    rows: list[tuple] = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            ts = int(datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
            rows.append((ts, float(parts[2]), float(parts[3]), float(parts[4]),
                         float(parts[5]), float(parts[6]), float(parts[7])))
        except (ValueError, TypeError):
            continue
    return rows


def checksum_matches(payload: bytes, checksum_text: str) -> bool:
    """The .CHECKSUM sibling is '<sha256>  <filename>'."""
    want = checksum_text.strip().split()[0].lower()
    return hashlib.sha256(payload).hexdigest() == want


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 60) -> bytes | None:
    """GET with backoff. None on 404 (a month that does not exist is ordinary)."""
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def archive_symbols(prefix: str = "data/spot/monthly/klines/") -> list[str]:
    """Every symbol the archive has ever held, delisted included (the point of using it)."""
    out: list[str] = []
    marker = ""
    while True:
        url = f"{S3_LIST}?delimiter=/&prefix={prefix}&max-keys=1000"
        if marker:
            url += f"&marker={marker}"
        body = _get(url, timeout=30)
        if not body:
            break
        page = re.findall(rf"<Prefix>{re.escape(prefix)}([^/]+)/</Prefix>", body.decode())
        out += page
        if "<IsTruncated>true</IsTruncated>" not in body.decode() or not page:
            break
        marker = f"{prefix}{page[-1]}/"
        time.sleep(0.05)
    return out


def months_from_listing(xml: str, symbol: str, interval: str) -> list[str]:
    """The 'YYYY-MM' parts an S3 listing page actually offers, ignoring .CHECKSUM siblings."""
    pat = rf"<Key>[^<]*/{re.escape(symbol)}-{re.escape(interval)}-(\d{{4}}-\d{{2}})\.zip</Key>"
    return sorted(set(re.findall(pat, xml)))


def months_available(symbol: str, interval: str) -> list[str]:
    """Ask the archive which months exist for this pair.

    One listing request replaces up to ~70 speculative downloads that would 404 because the
    pair had not listed yet. Over 723 symbols that is the difference between a ~50,000-request
    job and a ~20,000-request one, against a WAF that throttles bulk pullers.
    """
    prefix = f"data/spot/monthly/klines/{symbol}/{interval}/"
    body = _get(f"{S3_LIST}?prefix={prefix}&max-keys=1000", timeout=30)
    return months_from_listing(body.decode(), symbol, interval) if body else []


def fetch_zip(url: str) -> str | None:
    """Download a zip, verify its .CHECKSUM sibling, return the single member's text."""
    payload = _get(url)
    if payload is None:
        return None
    sig = _get(url + ".CHECKSUM", timeout=30)
    if sig and not checksum_matches(payload, sig.decode()):
        print(f"  CHECKSUM MISMATCH: {url}", file=sys.stderr)
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
        return zf.read(zf.namelist()[0]).decode()
    except (zipfile.BadZipFile, IndexError, UnicodeDecodeError):
        print(f"  UNREADABLE: {url}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL, trades INTEGER,
  PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_candles_ts ON candles(interval, ts);

CREATE TABLE IF NOT EXISTS metrics (
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  open_interest REAL, oi_value REAL, top_acct_ratio REAL, top_pos_ratio REAL,
  global_acct_ratio REAL, taker_ratio REAL,
  PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

-- Resume marker AND provenance: which archive parts are already loaded.
CREATE TABLE IF NOT EXISTS parts (
  kind TEXT NOT NULL, symbol TEXT NOT NULL, part TEXT NOT NULL,
  rows INTEGER, loaded_at TEXT,
  PRIMARY KEY (kind, symbol, part)
) WITHOUT ROWID;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def loaded_parts(db: sqlite3.Connection, kind: str) -> set[tuple[str, str]]:
    return {(s, p) for s, p in db.execute("SELECT symbol, part FROM parts WHERE kind=?", (kind,))}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_symbols(args) -> int:
    syms = archive_symbols()
    study = [s for s in syms if is_study_symbol(s, args.quote)]
    print(f"archive symbols: {len(syms)}   {args.quote} pairs fit for study: {len(study)}")
    print("first 10:", study[:10])
    return 0


def _do_klines(sym: str, months: list[str], interval: str) -> tuple[str, list[tuple], list[str]]:
    rows: list[tuple] = []
    done: list[str] = []
    offered = set(months_available(sym, interval))
    for mon in [m for m in months if m in offered]:
        url = f"{BASE}/data/spot/monthly/klines/{sym}/{interval}/{sym}-{interval}-{mon}.zip"
        text = fetch_zip(url)
        if text is None:
            continue                     # not listed yet, or not published
        parsed = parse_kline_csv(text)
        rows += [(sym, interval, *r) for r in parsed]
        done.append(mon)
    return sym, rows, done


def cmd_klines(args) -> int:
    db = connect(Path(args.out))
    months = month_range(args.start, args.end)
    syms = sorted(s for s in archive_symbols() if is_study_symbol(s, args.quote))
    if args.limit:
        syms = syms[: args.limit]
    have = loaded_parts(db, f"klines:{args.interval}")
    todo = {s: [m for m in months if (s, m) not in have] for s in syms}
    todo = {s: m for s, m in todo.items() if m}
    print(f"{len(syms)} symbols x {len(months)} months; {sum(len(m) for m in todo.values())} parts to fetch")

    done_syms = 0
    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = {pool.submit(_do_klines, s, m, args.interval): s for s, m in todo.items()}
        for fut in futures.as_completed(jobs):
            sym, rows, done = fut.result()
            if rows:
                db.executemany(
                    "INSERT OR IGNORE INTO candles(symbol,interval,ts,open,high,low,close,"
                    "volume,quote_volume,trades) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            db.executemany(
                "INSERT OR REPLACE INTO parts(kind,symbol,part,rows,loaded_at) VALUES (?,?,?,?,?)",
                [(f"klines:{args.interval}", sym, m, 0, datetime.now(timezone.utc).isoformat())
                 for m in done])
            db.commit()
            done_syms += 1
            if done_syms % 25 == 0:
                print(f"  {done_syms}/{len(todo)} symbols")

    n, first, last = db.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles WHERE interval=?", (args.interval,)).fetchone()
    sym_n = db.execute("SELECT COUNT(DISTINCT symbol) FROM candles WHERE interval=?",
                       (args.interval,)).fetchone()[0]
    def fmt(t):
        return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d") if t else "-"

    print(f"candles: {n} rows, {sym_n} symbols, {fmt(first)} .. {fmt(last)}")
    return 0


def _do_metrics(sym: str, days: list[str]) -> tuple[str, list[tuple], list[str]]:
    rows: list[tuple] = []
    done: list[str] = []
    for day in days:
        url = f"{BASE}/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{day}.zip"
        text = fetch_zip(url)
        if text is None:
            continue
        rows += [(sym, *r) for r in parse_metrics_csv(text)]
        done.append(day)
    return sym, rows, done


def cmd_metrics(args) -> int:
    """Futures OI / long-short / taker flow. Daily partitions only, so this is the expensive
    one: scope it with --top and a short window rather than pulling five years for 1000 pairs."""
    db = connect(Path(args.out))
    day = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    days = []
    while day <= end:
        days.append(day.strftime("%Y-%m-%d"))
        day = datetime.fromtimestamp(day.timestamp() + 86400)
    syms = sorted(s for s in archive_symbols("data/futures/um/daily/metrics/")
                  if is_study_symbol(s, args.quote))[: args.top]
    have = loaded_parts(db, "metrics")
    todo = {s: [d for d in days if (s, d) not in have] for s in syms}
    todo = {s: d for s, d in todo.items() if d}
    print(f"{len(syms)} symbols x {len(days)} days; {sum(len(d) for d in todo.values())} parts to fetch")

    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = {pool.submit(_do_metrics, s, d): s for s, d in todo.items()}
        for i, fut in enumerate(futures.as_completed(jobs), 1):
            sym, rows, done = fut.result()
            if rows:
                db.executemany(
                    "INSERT OR IGNORE INTO metrics(symbol,ts,open_interest,oi_value,"
                    "top_acct_ratio,top_pos_ratio,global_acct_ratio,taker_ratio) "
                    "VALUES (?,?,?,?,?,?,?,?)", rows)
            db.executemany(
                "INSERT OR REPLACE INTO parts(kind,symbol,part,rows,loaded_at) VALUES (?,?,?,?,?)",
                [("metrics", sym, d, 0, datetime.now(timezone.utc).isoformat()) for d in done])
            db.commit()
            if i % 10 == 0:
                print(f"  {i}/{len(todo)} symbols")
    print("metrics rows:", db.execute("SELECT COUNT(*) FROM metrics").fetchone()[0])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=str(DEFAULT_DB))
    p.add_argument("--quote", default="USDT")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("symbols", help="count what the archive holds")
    s.set_defaults(func=cmd_symbols)

    k = sub.add_parser("klines", help="download spot OHLCV")
    k.add_argument("--start", default="2021-01")
    k.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m"))
    k.add_argument("--interval", default="1d")
    k.add_argument("--limit", type=int, default=0, help="first N symbols (smoke test)")
    k.set_defaults(func=cmd_klines)

    m = sub.add_parser("metrics", help="download futures OI / long-short / taker flow")
    m.add_argument("--start", default="2024-01-01")
    m.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    m.add_argument("--top", type=int, default=100)
    m.set_defaults(func=cmd_metrics)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
