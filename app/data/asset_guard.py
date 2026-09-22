"""
ENTRY-only guard against delisting / high-risk / non-crypto bases (Binance spot).

Why: the KSS ladder ships with SL=0 on most books, so a delisting is a -100% event that no
exit logic can catch — the only defense is never opening the ladder in the first place.
``providers._is_excluded_base`` only drops a fixed stablecoin/fiat list; it has no notion of
Binance's own risk tags (Monitoring), tokenized stocks/ETFs (bStocks), commodity tokens
(tCommodities), wrapped duplicates (WBTC/WBETH), or an active delisting announcement.

This module is used ONLY on the new-entry path (universe filtering + the pre-open check in
``scanner._review_and_open``). It NEVER touches an existing session: rungs, take-profit,
trailing, the hard-SL guard and the deadline are all governed elsewhere and are completely
untouched by anything here — "exits are never gated" (see CLAUDE.md). The one side effect on
an ACTIVE session is informational: ``audit_held_symbols`` records+alerts once when a symbol
already held becomes flagged, so a human can decide whether to intervene manually.

Data sources (unofficial, unauthenticated Binance endpoints — treated as untrusted content,
never eval'd, always shape-checked):
  * product list: tags (Monitoring / bStocks / tCommodities / stablecoin) + asset name.
  * delisting announcements (CMS articles, catalogId=161): best-effort regex extraction of
    the bases named in a recent (<=30d) delist notice.

The last-good snapshot is cached in ``runtime_config`` (via ``app.runtime``'s generic KV
store) so a restart or an outage keeps blocking on the last known classification instead of
silently reopening the gate. See ``blocked_reason`` for the fail-open contract on staleness.
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from app.clock import utcnow
from app.config import settings

logger = logging.getLogger(__name__)

# --- network -----------------------------------------------------------------------------

_PRODUCTS_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"
_ARTICLE_LIST_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
_ARTICLE_DETAIL_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"

_HTTP_TIMEOUT = 10.0  # seconds — per connect/read op; unofficial endpoint, never hangs a tick
# httpx's `timeout=` on a stream bounds each individual socket read, not the WHOLE download —
# a slow server trickling one byte every 9s would never trip _HTTP_TIMEOUT while still taking
# minutes overall. This is a wall-clock ceiling on the entire streamed read loop in `_get_json`.
_TOTAL_TIMEOUT_SEC = 20.0
_MAX_RESPONSE_BYTES = 5_000_000  # sane ceiling; observed product list is ~666 KB
_DELIST_CATALOG_ID = 161
_DELIST_PAGE_SIZE = 20
_DELIST_LOOKBACK_DAYS = 30

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")

# --- classification tags/constants --------------------------------------------------------

TAG_MONITORING = "Monitoring"
TAG_STOCK = "bStocks"
TAG_COMMODITY = "tCommodities"
TAG_STABLE = "stablecoin"

# No product tag identifies a wrapped duplicate — only the asset name does ("Wrapped Bitcoin",
# "Wrapped Beacon ETH"), which needs a live snapshot to read. This static seed list is the part
# of "wrapped" classification that needs NO snapshot at all, so — like the manual denylist —
# it always applies, even with no snapshot ever fetched or a stale one.
WRAPPED_SEEDS = frozenset({"WBTC", "WBETH", "BETH"})

# Reasons keyed to the on/off knob that gates them. "stablecoin" (tag) and "delist_announced"
# have no dedicated knob — they always apply once asset_guard_enabled is True (see
# blocked_reason). Kept distinct from providers.py's own hardcoded stablecoin/fiat list, which
# is untouched by this module and always applies regardless of asset_guard state.
_KNOB_FOR_REASON = {
    "monitoring": "asset_guard_block_monitoring",
    "stock_token": "asset_guard_block_stock_tokens",
    "commodity": "asset_guard_block_commodities",
    "wrapped": "asset_guard_block_wrapped",
}

RUNTIME_KEY_SNAPSHOT = "asset_guard_snapshot"
RUNTIME_KEY_LAST_ATTEMPT = "asset_guard_last_attempt"
_FLAGGED_KEY_PREFIX = "asset_guard_flagged:"

# Debounce for the "stale/never-fetched" audit row — blocked_reason is called once per
# universe symbol per scan (dozens-hundreds of calls), so without this it would write one
# audit row per symbol per scan instead of one per outage.
_STALE_AUDIT_COOLDOWN_SEC = 600.0
_last_stale_audit_at: float = 0.0


class FetchError(RuntimeError):
    """A fetch/parse step failed. Always caught internally — callers keep the last-good state."""


def reset_stale_audit_throttle() -> None:
    """Clear the in-process stale-audit debounce (tests)."""
    global _last_stale_audit_at
    _last_stale_audit_at = 0.0


# --- HTTP + defensive parsing ---------------------------------------------------------------


def _get_json(url: str, params: dict | None = None) -> object:
    """One GET, defensively checked end to end. STREAMS the body (httpx.stream) so an oversized
    response is aborted as soon as it crosses ``_MAX_RESPONSE_BYTES`` instead of being fully
    downloaded and buffered first — cheap because httpx already streams the socket internally;
    this just stops reading early rather than materializing the whole body before measuring it.
    A ``Content-Length`` header is checked first when present (aborts before reading any body
    at all); the running-total check covers chunked/absent-header responses too. Also enforces
    ``_TOTAL_TIMEOUT_SEC`` as a wall-clock deadline across the WHOLE streamed read — httpx's own
    ``timeout=`` only bounds each individual socket read, so a slow trickle could otherwise run
    far longer overall while never tripping it. Raises FetchError on ANY problem (network,
    oversized body, non-JSON, deadline exceeded) — never returns a partial/poisoned result."""
    start = time.monotonic()
    try:
        with httpx.stream("GET", url, params=params, timeout=_HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > _MAX_RESPONSE_BYTES:
                        raise FetchError(
                            f"GET {url} response too large "
                            f"({content_length} bytes, Content-Length)"
                        )
                except ValueError:
                    pass  # a malformed header must not abort a request that may be fine
            chunks = bytearray()
            for chunk in resp.iter_bytes():
                if time.monotonic() - start > _TOTAL_TIMEOUT_SEC:
                    raise FetchError(
                        f"GET {url} exceeded overall deadline of {_TOTAL_TIMEOUT_SEC}s"
                    )
                chunks.extend(chunk)
                if len(chunks) > _MAX_RESPONSE_BYTES:
                    raise FetchError(
                        f"GET {url} response too large (>{_MAX_RESPONSE_BYTES} bytes, streamed)"
                    )
            body = bytes(chunks)
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001 — any transport/HTTP failure degrades the same way
        raise FetchError(f"GET {url} failed: {exc}") from exc
    try:
        return json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"GET {url} response not JSON: {exc}") from exc


def _valid_base(sym: object) -> bool:
    """Untrusted-content guard: a base symbol must look like a real ticker, not arbitrary text
    smuggled through an unofficial JSON endpoint."""
    return isinstance(sym, str) and bool(_SYMBOL_RE.match(sym))


def parse_products(payload: object) -> dict[str, dict]:
    """Pure parse: Binance product-list JSON -> ``{base: {"tags": [...], "name": str}}`` for
    USDT-quoted pairs with a well-formed base symbol. Any malformed/missing field on one item
    just drops that item — the whole payload never raises past this function."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        base, quote = item.get("b"), item.get("q")
        if quote != "USDT" or not _valid_base(base):
            continue
        base = base.upper()
        tags = item.get("tags")
        tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        name = item.get("an")
        out[base] = {"tags": tags, "name": name if isinstance(name, str) else ""}
    return out


# --- delisting announcements (best-effort) --------------------------------------------------

_DELIST_TITLE_RE = re.compile(r"delist|removal of spot trading pairs", re.IGNORECASE)
# "Binance Will Delist ICX, SCRT, STORJ on 2026-09-03" / "...Delist BTTC & POWR on..." /
# "...Delist U (Union) on..." — '&' and '(...)' are allowed in the captured chunk so the match
# doesn't simply fail to reach "on" (see extract_delisted_bases for how the parenthetical name
# text is then discarded rather than tokenized).
_DELIST_LIST_RE = re.compile(r"delist(?:ing)?\s+([A-Z0-9,&()\s/]+?)\s+on\b", re.IGNORECASE)

# Binance runs Margin/Futures/Simple Earn/Convert/Alpha/Loan delist-removal notices on a
# completely different schedule from SPOT — "Binance Margin Will Delist BTTC & POWR" does not
# touch the BTTC/USDT or POWR/USDT SPOT pair this bot actually trades (verified live
# 2026-09-21: both kept trading spot). Only a title with NO such product prefix counts.
_NON_SPOT_TITLE_RE = re.compile(
    r"\b(margin|futures?|simple\s+earn|convert|alpha|loan|savings|options?|"
    r"leveraged\s+tokens?)\b",
    re.IGNORECASE,
)


def _is_spot_scope_title(title: str) -> bool:
    """True unless the title names a non-SPOT product — see ``_NON_SPOT_TITLE_RE``."""
    return not _NON_SPOT_TITLE_RE.search(title)


# A genuine "Delist A, B, C on <date>" list is short (verified against real notices: usually
# 1-10 symbols). These caps reject a match that is implausibly long rather than trying to
# parse prose correctly — false NEGATIVES here are fine (best-effort supplement), false
# POSITIVES are not (they would refuse a clean coin's entry on a coincidental all-caps word).
_MAX_DELIST_LIST_CHARS = 150
_MAX_DELIST_LIST_TOKENS = 10


def _quote_asset() -> str:
    """The bot's own quote currency (e.g. "USDT") — read from the live data provider rather
    than hardcoded, so this module can never drift from what actually trades. Falls back to
    "USDT" (today's only configured venue/quote) on any failure."""
    try:
        from app.data.providers import data_provider

        return (data_provider().quote or "USDT").upper()
    except Exception:  # noqa: BLE001 — a provider hiccup must not break parsing
        return "USDT"


def extract_pair_mentions(text: str, quote: str | None = None) -> set[str]:
    """Bases named as an explicit "BASE/<quote>" pair being removed — the ONLY pattern this
    module trusts on article BODY text, and ONLY for the bot's OWN quote asset (default: read
    from the live provider, e.g. "USDT").

    Why quote-scoped: a "Notice of Removal of Spot Trading Pairs" typically removes a handful
    of SPECIFIC pairs (e.g. QNT/USDC, OPEN/FDUSD) while the base coin keeps trading against
    OTHER quotes, including USDT — verified live 2026-09-21 (BREV, LA, QNT, OPEN, SAGA were all
    named in such a notice yet all still TRADING vs USDT). Matching every quote asset
    previously blocked coins whose USDT pair was never actually touched.

    Why body-only (not title): every article detail page repeats generic template prose
    ("...delist and cease trading on...", "...delisting schedule may or may not apply to the
    products listed below...") that the "Delist A, B, C on" list pattern happily (and wrongly)
    matches — minting bogus bases out of ordinary English words. An explicit pair mention has
    no such failure mode (nothing in that boilerplate looks like "WORD/USDT").
    """
    if not text:
        return set()
    q = re.escape((quote or _quote_asset()).upper())
    pair_re = re.compile(rf"\b([A-Z0-9]{{2,20}})/{q}\b")
    return {m.group(1).upper() for m in pair_re.finditer(text) if _valid_base(m.group(1))}


def extract_delisted_bases(text: str) -> set[str]:
    """Best-effort regex extraction of base symbols from a delisting article TITLE (trusted —
    real titles are short, clean, and directly name the coins: "Binance Will Delist ACX, HFT,
    PIVX on 2026-08-17"). Combines the "Delist A, B, C on <date>" list pattern (coin-level, spot
    — not quote-scoped) with ``extract_pair_mentions`` (quote-scoped). Do NOT call this on
    article BODY text — see ``extract_pair_mentions``'s docstring for why the list pattern is
    unsafe there.
    """
    if not text:
        return set()
    bases: set[str] = set(extract_pair_mentions(text))
    for m in _DELIST_LIST_RE.finditer(text):
        # Drop "(Full Name)" annotations (e.g. "U (Union)") before tokenizing — the name text
        # is prose, not a symbol, and its uppercased form can coincidentally fit the symbol
        # shape ("Union" -> "UNION").
        chunk = re.sub(r"\([^)]*\)", "", m.group(1))
        if len(chunk) > _MAX_DELIST_LIST_CHARS:
            continue  # implausibly long for a real symbol list — likely unrelated prose
        parts = [p for p in re.split(r"[,&\s]+", chunk) if p]
        if len(parts) > _MAX_DELIST_LIST_TOKENS:
            continue
        for part in parts:
            base = part.split("/")[0].strip().upper()
            if _valid_base(base):
                bases.add(base)
    return bases


def _extract_article_text(payload: object) -> str:
    """Best-effort: pull plain text out of the CMS article-detail JSON's node tree. The shape
    is unofficial/undocumented, so this walks any dict/list collecting string values under
    title/body/text/content-ish keys rather than assuming one exact structure. Never raises."""
    texts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and k.lower() in {"title", "body", "text", "content"}:
                    texts.append(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    try:
        _walk(payload)
    except Exception:  # noqa: BLE001 — advisory parse, never allowed to raise
        return ""
    # Joined with ". " (a real sentence break for the "Delist A, B, C on <date>" pattern,
    # which forbids "." inside its captured chunk): a plain " " join merged unrelated text
    # nodes into one run-on string, letting "delist" from one sentence pair with an
    # unrelated "on" much later in another and mint bogus "symbols" out of ordinary prose
    # in between (observed live 2026-09-21: "AFOREMENTIONED", "PAIRS", "THE", ...). The
    # BASE/QUOTE pair pattern is unaffected either way — it never spans a separator.
    return ". ".join(texts)


def _article_recent_enough(article: dict) -> bool:
    """True unless the article carries a releaseDate that is clearly outside the lookback
    window. Missing/unparseable dates are treated as recent (best-effort — a title match with
    no readable date is still worth keeping rather than silently dropped)."""
    released = article.get("releaseDate")
    if not isinstance(released, (int, float)):
        return True
    try:
        ts_ms = released if released > 1e12 else released * 1000
        age = utcnow().timestamp() * 1000 - ts_ms
        return age <= _DELIST_LOOKBACK_DAYS * 86_400_000
    except Exception:  # noqa: BLE001
        return True


def fetch_delisted_bases() -> set[str] | None:
    """Best-effort: bases named in a delisting/removal announcement from the last
    ``_DELIST_LOOKBACK_DAYS`` days.

    Returns ``None`` — instead of an empty set — when the article-list fetch failed, or the
    article list itself came back empty/unparseable: in EITHER case there is no evidence
    "nothing is delisted", only that this endpoint didn't answer usefully this round, and the
    caller (``fetch_snapshot``/``refresh_if_due``) carries the previous last-good delisted set
    forward instead of erasing it (per-part trust — this is a SUPPLEMENT to the tag-based
    classes, and a broken/quiet fetch here must never silently un-flag a real delisting).

    An empty ``set()`` is returned when articles WERE fetched and parsed successfully but none
    matched the delist-keyword / spot-scope / recency filters — a normal, trustworthy "nothing
    new this round" result that DOES replace the previous list (delistings older than the
    lookback window are meant to roll off).
    """
    try:
        payload = _get_json(
            _ARTICLE_LIST_URL,
            params={"type": 1, "catalogId": _DELIST_CATALOG_ID, "pageNo": 1,
                    "pageSize": _DELIST_PAGE_SIZE},
        )
    except FetchError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    articles: object = None
    if isinstance(data, list):
        articles = data
    elif isinstance(data, dict):
        if isinstance(data.get("articles"), list):
            articles = data["articles"]
        else:
            # Observed live shape: data.catalogs[*].articles (verified 2026-09-21).
            catalogs = data.get("catalogs")
            if isinstance(catalogs, list):
                articles = [
                    art
                    for cat in catalogs
                    if isinstance(cat, dict) and isinstance(cat.get("articles"), list)
                    for art in cat["articles"]
                ]
    if not isinstance(articles, list) or not articles:
        return None  # empty/unparseable article list — not "no delistings", unknown
    bases: set[str] = set()
    for art in articles:
        if not isinstance(art, dict):
            continue
        title = art.get("title")
        if not isinstance(title, str) or not _DELIST_TITLE_RE.search(title):
            continue
        if not _is_spot_scope_title(title):
            continue  # Margin/Futures/Simple Earn/Convert/Alpha/Loan — not a SPOT delist
        if not _article_recent_enough(art):
            continue
        bases |= extract_delisted_bases(title)
        code = art.get("code")
        if isinstance(code, str) and code:
            try:
                detail = _get_json(_ARTICLE_DETAIL_URL, params={"articleCode": code})
            except FetchError:
                continue
            # Body text: pair-mentions ONLY (see extract_pair_mentions's docstring) — the
            # "Delist A, B, C on" list pattern is title-only.
            bases |= extract_pair_mentions(_extract_article_text(detail))
    return bases


# --- snapshot fetch + persistence -----------------------------------------------------------


def fetch_snapshot() -> dict:
    """One fetch cycle: the product list (required — raises FetchError on failure) plus
    delisting bases (best-effort). ``delisted`` is ``None`` in the returned dict when the
    article fetch failed/was unusable this round (see ``fetch_delisted_bases``) — the caller
    (``refresh_if_due``) is responsible for carrying the previous last-good list forward in
    that case rather than saving a snapshot that silently erases it."""
    payload = _get_json(_PRODUCTS_URL, params={"includeEtf": "true"})
    products = parse_products(payload)
    try:
        delisted = fetch_delisted_bases()
    except Exception:  # noqa: BLE001 — best-effort half of the snapshot must never poison the rest
        logger.warning("asset_guard: delisting article fetch failed", exc_info=True)
        delisted = None
    return {
        "fetched_at": utcnow().isoformat(),
        "products": products,
        "delisted": sorted(delisted) if delisted is not None else None,
    }


def _load_snapshot(db) -> dict | None:
    from app import runtime

    raw = runtime.get(db, RUNTIME_KEY_SNAPSHOT)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _save_snapshot(db, snapshot: dict) -> None:
    from app import runtime

    runtime.set(db, RUNTIME_KEY_SNAPSHOT, json.dumps(snapshot))


# A 200 response with valid-but-empty/renamed JSON (data: [], a renamed field, an A/B endpoint
# variant) parses cleanly to ZERO products — fetch_snapshot has no way to tell that apart from
# "no requests currently pass any USDT product", so accepting it would overwrite a good
# snapshot's classifications with nothing and silently disable every tag-based class. A crash
# below half the last-good count is treated the same way (a partial/broken response, not a
# real universe shrink — Binance does not delist half its USDT market between two refreshes).
_SUSPECT_MIN_RATIO = 0.5
# Persisted (not time-based) episode markers: refresh runs on `asset_guard_refresh_min`
# (default 60min) — a 10-minute time cooldown would still fire a fresh audit row on every
# refresh during a multi-hour outage. "Once per episode" instead: set the first time the
# episode is observed, cleared the moment a normal/trustworthy result is accepted again.
RUNTIME_KEY_SUSPECT_EPISODE = "asset_guard_suspect_episode"
RUNTIME_KEY_DELIST_FAIL_EPISODE = "asset_guard_delist_fail_episode"


def reset_suspect_audit_throttle() -> None:
    """No-op kept for backward compatibility with older callers/tests — the debounce is now
    persisted per-episode (``RUNTIME_KEY_SUSPECT_EPISODE``), not an in-process timer."""


def _audit_suspect_once(db, count: int, prev_count: int) -> None:
    from app import audit, runtime

    if runtime.get(db, RUNTIME_KEY_SUSPECT_EPISODE):
        return  # already audited this episode
    runtime.set(db, RUNTIME_KEY_SUSPECT_EPISODE, "1")
    audit.log(db, "asset_guard", "asset_guard_suspect_response",
              product_count=count, previous_count=prev_count)


def _is_snapshot_trustworthy(db, snapshot: dict) -> bool:
    """False for a freshly-fetched snapshot whose product count is 0, or has crashed to under
    ``_SUSPECT_MIN_RATIO`` of the previous last-good snapshot's count — see the module note
    above ``_SUSPECT_MIN_RATIO``. An ``asset_guard_suspect_response`` audit row is written once
    per outage EPISODE (persisted — survives a restart — and cleared the next time a
    trustworthy snapshot is accepted, not on a fixed timer)."""
    from app import runtime

    count = len(snapshot.get("products") or {})
    prev = _load_snapshot(db)
    prev_count = len(prev.get("products") or {}) if prev else 0
    suspect = count == 0 or (prev_count > 0 and count < prev_count * _SUSPECT_MIN_RATIO)
    if suspect:
        _audit_suspect_once(db, count, prev_count)
    elif runtime.get(db, RUNTIME_KEY_SUSPECT_EPISODE):
        runtime.set(db, RUNTIME_KEY_SUSPECT_EPISODE, "")  # episode over
    return not suspect


def _snapshot_age_hours(snapshot: dict | None) -> float | None:
    """Hours since *snapshot* was fetched, or None if it doesn't exist / has no readable
    timestamp. Pure — takes an already-loaded snapshot so a batch classification (e.g.
    ``filter_blocked``) never re-parses the cached JSON once per symbol."""
    if not snapshot:
        return None
    try:
        from datetime import datetime

        fetched_at = datetime.fromisoformat(snapshot["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (utcnow() - fetched_at).total_seconds() / 3600.0


def snapshot_age_hours(db) -> float | None:
    """Hours since the last GOOD snapshot, or None if one has never been saved."""
    return _snapshot_age_hours(_load_snapshot(db))


def _snapshot_is_stale(snapshot: dict | None) -> bool:
    age = _snapshot_age_hours(snapshot)
    return age is None or age > settings.asset_guard_max_stale_h


def _carry_forward_delisted_if_missing(db, snapshot: dict) -> None:
    """Mutates *snapshot* in place: when ``snapshot["delisted"]`` is ``None`` (the article
    fetch failed, or returned an empty/unparseable article list — see
    ``fetch_delisted_bases``), carry the PREVIOUS last-good delisted list forward instead of
    saving an empty one. A debounced (once-per-episode) ``asset_guard_delist_fetch_failed``
    audit row is written only when there was actually something to lose (the previous list was
    non-empty) — losing nothing is not worth an alert."""
    from app import runtime

    if snapshot.get("delisted") is not None:
        if runtime.get(db, RUNTIME_KEY_DELIST_FAIL_EPISODE):
            runtime.set(db, RUNTIME_KEY_DELIST_FAIL_EPISODE, "")  # episode over
        return
    prev = _load_snapshot(db)
    prev_delisted = list(prev.get("delisted") or []) if prev else []
    snapshot["delisted"] = prev_delisted
    if prev_delisted and not runtime.get(db, RUNTIME_KEY_DELIST_FAIL_EPISODE):
        runtime.set(db, RUNTIME_KEY_DELIST_FAIL_EPISODE, "1")
        from app import audit

        audit.log(db, "asset_guard", "asset_guard_delist_fetch_failed",
                  carried_forward=len(prev_delisted))


def refresh_if_due(db) -> bool:
    """Refresh the cached snapshot if ``asset_guard_refresh_min`` minutes have passed since the
    last ATTEMPT (success or failure) — gated on the last attempt, not the last success, so a
    persistent outage keeps retrying on schedule instead of hammering the endpoint every tick.
    Never raises: on failure the last-good snapshot (if any) is left exactly as it was.
    Returns True iff a fetch was attempted this call."""
    from app import runtime

    if not settings.asset_guard_enabled:
        return False
    last_attempt = runtime.get(db, RUNTIME_KEY_LAST_ATTEMPT)
    if last_attempt:
        try:
            from datetime import datetime

            elapsed_min = (utcnow() - datetime.fromisoformat(last_attempt)).total_seconds() / 60.0
            if elapsed_min < settings.asset_guard_refresh_min:
                return False
        except ValueError:
            pass  # a corrupt stamp must not wedge refresh off forever
    runtime.set(db, RUNTIME_KEY_LAST_ATTEMPT, utcnow().isoformat())
    try:
        snapshot = fetch_snapshot()
    except FetchError as exc:
        logger.warning("asset_guard refresh failed (keeping last-good snapshot): %s", exc)
        return True
    if not _is_snapshot_trustworthy(db, snapshot):
        logger.warning("asset_guard: suspect response (keeping last-good snapshot)")
        return True
    _carry_forward_delisted_if_missing(db, snapshot)
    _save_snapshot(db, snapshot)
    return True


# --- classification ---------------------------------------------------------------------

_DENYLIST_MAX_ENTRIES = 200


def _denylist() -> set[str]:
    raw = settings.asset_guard_denylist or ""
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


def is_valid_denylist(raw: str | None) -> bool:
    """True if *raw* is a well-formed comma-separated denylist for the settings endpoint: each
    non-empty token matches the same symbol shape used everywhere else in this module
    (``^[A-Z0-9]{2,20}$``, case-insensitive), and there are at most ``_DENYLIST_MAX_ENTRIES``.
    An operator typo here would otherwise either silently fail to block anything, or get stored
    verbatim and never match a real (normalized) base."""
    if not raw:
        return True
    tokens = [p.strip() for p in raw.split(",") if p.strip()]
    if len(tokens) > _DENYLIST_MAX_ENTRIES:
        return False
    return all(_valid_base(t.upper()) for t in tokens)


def _tag_reason(product: dict | None) -> str | None:
    """Pure per-symbol classification from ONE product record — no knobs, no staleness. The
    shared core that both ``blocked_reason`` and ``blocked_symbols_by_reason`` build on."""
    if not product:
        return None
    tags = product.get("tags") or []
    name = (product.get("name") or "").lower()
    if TAG_MONITORING in tags:
        return "monitoring"
    if TAG_STOCK in tags:
        return "stock_token"
    if TAG_COMMODITY in tags:
        return "commodity"
    if TAG_STABLE in tags:
        return "stablecoin"
    if "wrapped" in name:
        return "wrapped"
    return None


def _audit_stale_once(db, never_fetched: bool) -> None:
    global _last_stale_audit_at
    now = time.monotonic()
    if now - _last_stale_audit_at < _STALE_AUDIT_COOLDOWN_SEC:
        return
    _last_stale_audit_at = now
    from app import audit

    audit.log(db, "asset_guard", "asset_guard_stale", never_fetched=never_fetched,
              max_stale_h=settings.asset_guard_max_stale_h)


def _classify(base: str, snapshot: dict | None, stale: bool) -> str | None:
    """Pure per-symbol classification given an ALREADY-LOADED snapshot and its precomputed
    staleness — the shared core of ``blocked_reason`` (single symbol) and ``filter_blocked``
    (a whole batch, loading the snapshot only once). Order:

      1. ALWAYS-APPLY classes, independent of the snapshot's existence/freshness: the manual
         denylist and the wrapped static seed list (WBTC/WBETH/BETH).
      2. Tag-based classes (monitoring/stock_token/commodity/stablecoin/wrapped-by-name) and
         the best-effort delisting list — these need a snapshot. Missing/stale FAILS OPEN for
         these classes (the caller is responsible for the debounced ``asset_guard_stale`` audit
         row — see ``blocked_reason``/``filter_blocked``).
    """
    if base in _denylist():
        return "denylist"
    if base in WRAPPED_SEEDS and settings.asset_guard_block_wrapped:
        return "wrapped"
    if snapshot is None or stale:
        return None  # fail-open: no verified evidence to classify tag-based classes by
    product = snapshot.get("products", {}).get(base)
    reason = _tag_reason(product)
    if reason is not None:
        knob = _KNOB_FOR_REASON.get(reason)
        if knob is not None and not getattr(settings, knob):
            reason = None
    if reason:
        return reason
    if base in (snapshot.get("delisted") or []):
        return "delist_announced"
    return None


def blocked_reason(db, symbol: str) -> str | None:
    """ENTRY-only: the reason a NEW candidate/session for *symbol* must be refused, or None to
    allow it. NEVER call this on an exit/existing-session path (see module docstring). The
    ``asset_guard_enabled`` master switch bypasses everything when False; see ``_classify`` for
    the classification order. Scanning many symbols in a loop? Use ``filter_blocked`` instead —
    it loads the snapshot once for the whole batch."""
    if not settings.asset_guard_enabled:
        return None
    base = (symbol or "").upper()
    if not base:
        return None
    snapshot = _load_snapshot(db)
    stale = _snapshot_is_stale(snapshot)
    if snapshot is None or stale:
        _audit_stale_once(db, snapshot is None)
    return _classify(base, snapshot, stale)


def filter_blocked(db, symbols: list[str]) -> list[str]:
    """*symbols* with every asset-guard-blocked base removed — the batch form of
    ``blocked_reason`` (keep the ones that come back None), loading the cached snapshot ONCE
    for the whole list instead of once per symbol. Meant for a hot path over the full scanned
    universe (``scanner._universe``) so the guard is applied BEFORE the ``scan_max_symbols``
    top-N cut: filtering after that cut would let blocked coins consume scan slots that a
    healthy coin further down the volume ranking could have used instead."""
    if not settings.asset_guard_enabled:
        return list(symbols)
    snapshot = _load_snapshot(db)
    stale = _snapshot_is_stale(snapshot)
    if snapshot is None or stale:
        _audit_stale_once(db, snapshot is None)
    return [s for s in symbols if _classify((s or "").upper(), snapshot, stale) is None]


def blocked_symbols_by_reason(db) -> dict[str, list[str]]:
    """Every base the current (possibly stale) snapshot classifies, grouped by reason — for
    the settings-page transparency table. Ignores the per-class on/off knobs (shows the full
    potential set) and does NOT apply the staleness fail-open — this is a snapshot inspector,
    not the live gate (``blocked_reason`` is authoritative for what actually blocks an entry)."""
    out: dict[str, list[str]] = {}
    for base in sorted(_denylist()):
        out.setdefault("denylist", []).append(base)
    for base in sorted(WRAPPED_SEEDS):
        out.setdefault("wrapped", []).append(base)
    snapshot = _load_snapshot(db)
    if snapshot:
        for base, product in snapshot.get("products", {}).items():
            reason = _tag_reason(product)
            if reason:
                out.setdefault(reason, []).append(base)
        for base in snapshot.get("delisted") or []:
            out.setdefault("delist_announced", []).append(base)
    for reason, bases in out.items():
        out[reason] = sorted(set(bases))
    return out


def audit_held_symbols(db) -> list[str]:
    """For every symbol with an ACTIVE KSS session, note (once per symbol+reason) when it
    becomes flagged. NEVER touches the session itself — rungs, take-profit, trailing, the
    hard-SL guard and the deadline are governed entirely elsewhere and are untouched here; this
    only records an audit row per symbol and fires ONE aggregated risk alert per call (rather
    than one message per symbol — a batch of newly-flagged coins in the same tick, e.g. right
    after a snapshot refresh, would otherwise flood the channel) so a human can decide whether
    to act manually.

    Dedup is persisted (survives a restart) via a ``asset_guard_flagged:<symbol>`` runtime key
    holding the last-alerted reason: a reason CHANGE (e.g. monitoring -> delist_announced)
    re-alerts, a still-flagged coin does not, and the key is CLEARED (not merely left stale)
    the moment a FRESH, TRUSTWORTHY snapshot shows the symbol is no longer flagged — so if it
    is flagged again later (including with the SAME reason) that counts as a fresh episode and
    alerts again, instead of staying silently deduped forever against a flag that already
    cleared once. Deliberately NOT cleared while the snapshot is missing/stale: ``blocked_reason``
    fails open (returns None) in that case too, but that is "unknown", not "cleared" — clearing
    on it would un-dedupe every previously-flagged symbol for the whole outage, then re-alert
    all of them at once the moment the snapshot recovers.

    Returns the symbols that newly fired this call."""
    from app import runtime
    from app.models import SESSION_ACTIVE, KssSession

    if not settings.asset_guard_enabled:
        return []
    snapshot = _load_snapshot(db)
    trustworthy_now = snapshot is not None and not _snapshot_is_stale(snapshot)
    symbols = {row[0] for row in db.query(KssSession.symbol)
               .filter(KssSession.status == SESSION_ACTIVE).all()}
    fired: list[tuple[str, str]] = []
    for symbol in symbols:
        reason = blocked_reason(db, symbol)
        key = f"{_FLAGGED_KEY_PREFIX}{symbol}"
        if not reason:
            if trustworthy_now and runtime.get(db, key):
                runtime.set(db, key, "")  # flag cleared — reset dedupe for a future re-flag
            continue
        if runtime.get(db, key) == reason:
            continue  # already alerted for this exact reason, still active
        runtime.set(db, key, reason)
        fired.append((symbol, reason))
    if not fired:
        return []
    from app import audit, notify

    for symbol, reason in fired:
        audit.log(db, "asset_guard", "held_symbol_flagged", entity=symbol, reason=reason)
    lines = "\n".join(f"- {symbol}: {reason}" for symbol, reason in sorted(fired))
    notify.event(
        "risk",
        f"⚠️ {len(fired)} session ACTIVE vừa bị gắn cờ rủi ro:\n{lines}\n"
        "Session hiện tại KHÔNG bị đụng vào — rung/TP/exit vẫn chạy bình thường; "
        "đây chỉ là cảnh báo để cân nhắc can thiệp thủ công.",
    )
    return [symbol for symbol, _ in fired]
