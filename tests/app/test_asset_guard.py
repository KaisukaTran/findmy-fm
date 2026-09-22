"""Entry-only asset guard (app/data/asset_guard.py) — delisting/high-risk/non-crypto bases.

All HTTP is mocked (``httpx.stream`` monkeypatched) — this suite never touches the network.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.config import settings
from app.data import asset_guard
from app.models import SESSION_ACTIVE, AuditLog, KssSession, KssWave, PendingOrder

# --- fixtures ------------------------------------------------------------------------------

_SAMPLE_PRODUCTS = {
    "code": "000000",
    "data": [
        {"s": "AVAUSDT", "b": "AVA", "q": "USDT", "an": "Avalanche", "tags": ["Monitoring"]},
        {"s": "NVDABUSDT", "b": "NVDAB", "q": "USDT", "an": "NVDA Tokenized Stock",
         "tags": ["bStocks"]},
        {"s": "PAXGUSDT", "b": "PAXG", "q": "USDT", "an": "PAX Gold", "tags": ["tCommodities"]},
        {"s": "WBTCUSDT", "b": "WBTC", "q": "USDT", "an": "Wrapped Bitcoin", "tags": []},
        {"s": "RLUSDUSDT", "b": "RLUSD", "q": "USDT", "an": "Ripple USD", "tags": ["stablecoin"]},
        {"s": "BTCUSDT", "b": "BTC", "q": "USDT", "an": "Bitcoin", "tags": []},
    ],
}


class _FakeResp:
    """Stands in for the context-manager object ``httpx.stream(...)`` yields."""

    def __init__(self, content: bytes, status_ok: bool = True, headers: dict | None = None):
        self.content = content
        self._status_ok = status_ok
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("boom")

    def iter_bytes(self):
        yield self.content


def _json_resp(payload, **kw) -> _FakeResp:
    return _FakeResp(json.dumps(payload).encode(), **kw)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    asset_guard.reset_stale_audit_throttle()
    asset_guard.reset_suspect_audit_throttle()
    # Pin the bot's quote asset deterministically — real classification always resolves it via
    # the live data provider, which these tests never touch.
    monkeypatch.setattr(asset_guard, "_quote_asset", lambda: "USDT")
    yield
    asset_guard.reset_stale_audit_throttle()
    asset_guard.reset_suspect_audit_throttle()


def _seed_fresh_snapshot(db, products=None, delisted=None):
    snap = {
        "fetched_at": asset_guard.utcnow().isoformat(),
        "products": products if products is not None else asset_guard.parse_products(_SAMPLE_PRODUCTS),
        "delisted": sorted(delisted or []),
    }
    asset_guard._save_snapshot(db, snap)
    return snap


# --- classification ---------------------------------------------------------------------


def test_parse_products_classifies_tags_and_names():
    products = asset_guard.parse_products(_SAMPLE_PRODUCTS)
    assert asset_guard._tag_reason(products["AVA"]) == "monitoring"
    assert asset_guard._tag_reason(products["NVDAB"]) == "stock_token"
    assert asset_guard._tag_reason(products["PAXG"]) == "commodity"
    assert asset_guard._tag_reason(products["WBTC"]) == "wrapped"  # name-based
    assert asset_guard._tag_reason(products["RLUSD"]) == "stablecoin"
    assert asset_guard._tag_reason(products["BTC"]) is None


def test_blocked_reason_uses_cached_snapshot(db):
    _seed_fresh_snapshot(db)
    assert asset_guard.blocked_reason(db, "AVA") == "monitoring"
    assert asset_guard.blocked_reason(db, "NVDAB") == "stock_token"
    assert asset_guard.blocked_reason(db, "PAXG") == "commodity"
    assert asset_guard.blocked_reason(db, "WBTC") == "wrapped"
    assert asset_guard.blocked_reason(db, "RLUSD") == "stablecoin"
    assert asset_guard.blocked_reason(db, "BTC") is None
    assert asset_guard.blocked_reason(db, "btc") is None  # case-insensitive


def test_wrapped_seed_list_always_applies_even_without_snapshot(db):
    # No snapshot at all — WBETH/BETH are not in the sample product tags, only the static seed.
    assert asset_guard.blocked_reason(db, "WBETH") == "wrapped"
    assert asset_guard.blocked_reason(db, "BETH") == "wrapped"


def test_manual_denylist_always_applies_even_without_snapshot(db, monkeypatch):
    monkeypatch.setattr(settings, "asset_guard_denylist", "FTT,LUNA,LUNC")
    assert asset_guard.blocked_reason(db, "FTT") == "denylist"
    assert asset_guard.blocked_reason(db, "luna") == "denylist"
    assert asset_guard.blocked_reason(db, "BTC") is None


def test_delist_announced_reason(db):
    _seed_fresh_snapshot(db, delisted={"ICX", "SCRT"})
    assert asset_guard.blocked_reason(db, "ICX") == "delist_announced"
    assert asset_guard.blocked_reason(db, "SCRT") == "delist_announced"


def test_is_valid_denylist():
    assert asset_guard.is_valid_denylist("") is True
    assert asset_guard.is_valid_denylist(None) is True
    assert asset_guard.is_valid_denylist("FTT,LUNA,LUNC") is True
    assert asset_guard.is_valid_denylist("ftt, luna") is True
    assert asset_guard.is_valid_denylist("bad-symbol!") is False
    assert asset_guard.is_valid_denylist("way-too-long-to-ever-be-a-real-ticker-symbol") is False
    over = ",".join(f"SYM{i}" for i in range(201))
    at_cap = ",".join(f"SYM{i}" for i in range(200))
    assert asset_guard.is_valid_denylist(over) is False
    assert asset_guard.is_valid_denylist(at_cap) is True


def test_set_kss_settings_rejects_malformed_denylist(db):
    from app import runtime

    before = settings.asset_guard_denylist
    result = runtime.set_kss_settings(db, {"asset_guard_denylist": "bad-symbol!"})
    assert settings.asset_guard_denylist == before  # rejected, unchanged
    assert result["asset_guard_denylist"] == before


def test_set_kss_settings_accepts_valid_denylist(db):
    from app import runtime

    result = runtime.set_kss_settings(db, {"asset_guard_denylist": "FOO,BAR"})
    assert result["asset_guard_denylist"] == "FOO,BAR"


def test_kss_settings_endpoint_rejects_invalid_denylist_with_400(db):
    """Round-2 item 6: the HTTP endpoint must reject loudly (400 + message), like every other
    rejected edit on this endpoint — not silently drop the field and return 200."""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    before = settings.asset_guard_denylist
    with TestClient(fastapi_app) as c:
        r = c.post("/api/kss-settings", json={"asset_guard_denylist": "bad-symbol!"})
    assert r.status_code == 400
    assert "asset_guard_denylist" in r.json()["detail"]
    assert settings.asset_guard_denylist == before  # unchanged


def test_kss_settings_endpoint_accepts_valid_denylist(db):
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as c:
        r = c.post("/api/kss-settings", json={"asset_guard_denylist": "FOO,BAR"})
    assert r.status_code == 200
    assert r.json()["asset_guard_denylist"] == "FOO,BAR"


# --- knobs off ----------------------------------------------------------------------------


def test_master_switch_off_allows_everything(db, monkeypatch):
    _seed_fresh_snapshot(db)
    monkeypatch.setattr(settings, "asset_guard_enabled", False)
    for sym in ("AVA", "NVDAB", "PAXG", "WBTC", "RLUSD", "WBETH", "FTT"):
        assert asset_guard.blocked_reason(db, sym) is None


def test_per_class_knob_off_allows_only_that_class(db, monkeypatch):
    _seed_fresh_snapshot(db)
    monkeypatch.setattr(settings, "asset_guard_block_monitoring", False)
    assert asset_guard.blocked_reason(db, "AVA") is None
    assert asset_guard.blocked_reason(db, "NVDAB") == "stock_token"  # untouched


def test_wrapped_knob_off_allows_both_seed_and_tag(db, monkeypatch):
    _seed_fresh_snapshot(db)
    monkeypatch.setattr(settings, "asset_guard_block_wrapped", False)
    assert asset_guard.blocked_reason(db, "WBTC") is None
    assert asset_guard.blocked_reason(db, "WBETH") is None


# --- staleness / outage ---------------------------------------------------------------------


def test_never_fetched_fails_open_for_tags_but_denylist_still_blocks(db, monkeypatch):
    monkeypatch.setattr(settings, "asset_guard_denylist", "FTT")
    assert asset_guard.blocked_reason(db, "AVA") is None  # no snapshot ever -> fail-open
    assert asset_guard.blocked_reason(db, "FTT") == "denylist"
    assert asset_guard.blocked_reason(db, "WBTC") == "wrapped"
    assert db.query(AuditLog).filter_by(action="asset_guard_stale").count() == 1


def test_stale_snapshot_fails_open_for_tags_only(db, monkeypatch):
    old = asset_guard.utcnow() - timedelta(hours=200)
    snap = {"fetched_at": old.isoformat(),
            "products": asset_guard.parse_products(_SAMPLE_PRODUCTS), "delisted": []}
    asset_guard._save_snapshot(db, snap)
    monkeypatch.setattr(settings, "asset_guard_max_stale_h", 48.0)
    assert asset_guard.blocked_reason(db, "AVA") is None  # stale -> fail-open
    assert asset_guard.blocked_reason(db, "WBTC") == "wrapped"  # static, unaffected by staleness


def test_stale_audit_is_debounced(db):
    asset_guard.blocked_reason(db, "AVA")
    asset_guard.blocked_reason(db, "PAXG")
    asset_guard.blocked_reason(db, "NVDAB")
    assert db.query(AuditLog).filter_by(action="asset_guard_stale").count() == 1


def test_outage_keeps_last_good_snapshot(db, monkeypatch):
    _seed_fresh_snapshot(db)
    assert asset_guard.blocked_reason(db, "AVA") == "monitoring"

    def _boom(method, url, params=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(asset_guard.httpx, "stream", _boom)
    refreshed = asset_guard.refresh_if_due(db)
    assert refreshed is True  # an attempt WAS made
    # Last-good snapshot must be untouched.
    assert asset_guard.blocked_reason(db, "AVA") == "monitoring"


def test_refresh_respects_interval(db, monkeypatch):
    calls = {"n": 0}

    def _fake_stream(method, url, params=None, timeout=None):
        calls["n"] += 1
        if url == asset_guard._PRODUCTS_URL:
            return _json_resp(_SAMPLE_PRODUCTS)
        return _json_resp({"data": []})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    monkeypatch.setattr(settings, "asset_guard_refresh_min", 60)
    assert asset_guard.refresh_if_due(db) is True
    first_calls = calls["n"]
    assert first_calls > 0
    # Immediately due again -> should NOT refetch (interval not elapsed).
    assert asset_guard.refresh_if_due(db) is False
    assert calls["n"] == first_calls


def test_refresh_noop_when_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "asset_guard_enabled", False)
    assert asset_guard.refresh_if_due(db) is False
    assert asset_guard.snapshot_age_hours(db) is None


# --- suspect-response guard (silent-disable protection) -------------------------------------


def test_suspect_zero_products_keeps_last_good(db, monkeypatch):
    _seed_fresh_snapshot(db)
    assert asset_guard.blocked_reason(db, "AVA") == "monitoring"

    def _fake_stream(method, url, params=None, timeout=None):
        return _json_resp({"data": []})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    refreshed = asset_guard.refresh_if_due(db)
    assert refreshed is True
    assert asset_guard.blocked_reason(db, "AVA") == "monitoring"  # unchanged
    assert db.query(AuditLog).filter_by(action="asset_guard_suspect_response").count() == 1


def test_suspect_below_half_previous_count_keeps_last_good(db, monkeypatch):
    _seed_fresh_snapshot(db)  # 6 products
    assert len(asset_guard._load_snapshot(db)["products"]) == 6
    tiny_payload = {"data": [{"b": "BTC", "q": "USDT", "an": "Bitcoin", "tags": []}]}  # 1 < 6*0.5

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._PRODUCTS_URL:
            return _json_resp(tiny_payload)
        return _json_resp({"data": []})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    asset_guard.refresh_if_due(db)
    assert asset_guard.blocked_reason(db, "AVA") == "monitoring"  # original snapshot untouched
    assert db.query(AuditLog).filter_by(action="asset_guard_suspect_response").count() == 1


def test_healthy_refresh_replaces_snapshot(db, monkeypatch):
    _seed_fresh_snapshot(db)

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._PRODUCTS_URL:
            return _json_resp(_SAMPLE_PRODUCTS)  # same count -> trustworthy
        return _json_resp({"data": []})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    asset_guard.refresh_if_due(db)
    assert db.query(AuditLog).filter_by(action="asset_guard_suspect_response").count() == 0


def test_suspect_audit_is_once_per_episode_not_per_refresh(db):
    """Round-2: a 10-minute time cooldown would still re-fire on every asset_guard_refresh_min
    (default 60min) during a multi-hour outage. Must be once per EPISODE instead."""
    _seed_fresh_snapshot(db)  # 6 products saved
    bad = {"products": {}, "fetched_at": asset_guard.utcnow().isoformat(), "delisted": []}
    assert asset_guard._is_snapshot_trustworthy(db, bad) is False
    assert asset_guard._is_snapshot_trustworthy(db, bad) is False
    assert asset_guard._is_snapshot_trustworthy(db, bad) is False
    assert db.query(AuditLog).filter_by(action="asset_guard_suspect_response").count() == 1


def test_suspect_episode_clears_on_trustworthy_snapshot_then_can_refire(db):
    from app import runtime

    _seed_fresh_snapshot(db)
    bad = {"products": {}, "fetched_at": asset_guard.utcnow().isoformat(), "delisted": []}
    good = {"products": asset_guard.parse_products(_SAMPLE_PRODUCTS),
            "fetched_at": asset_guard.utcnow().isoformat(), "delisted": []}

    assert asset_guard._is_snapshot_trustworthy(db, bad) is False
    assert runtime.get(db, asset_guard.RUNTIME_KEY_SUSPECT_EPISODE) == "1"
    assert asset_guard._is_snapshot_trustworthy(db, good) is True
    assert runtime.get(db, asset_guard.RUNTIME_KEY_SUSPECT_EPISODE) == ""
    # A NEW suspect episode after recovery must audit again — not deduped forever.
    assert asset_guard._is_snapshot_trustworthy(db, bad) is False
    assert db.query(AuditLog).filter_by(action="asset_guard_suspect_response").count() == 2


# --- untrusted-content defense ---------------------------------------------------------------


def test_malformed_json_shapes_are_ignored():
    assert asset_guard.parse_products(None) == {}
    assert asset_guard.parse_products({"data": "not-a-list"}) == {}
    assert asset_guard.parse_products({"data": ["not-a-dict"]}) == {}
    assert asset_guard.parse_products({"data": [{"b": "OK", "q": "USDT"}]}) == {
        "OK": {"tags": [], "name": ""}
    }


def test_bad_symbol_shapes_are_dropped():
    payload = {"data": [
        {"b": "bad-symbol!", "q": "USDT", "tags": []},
        {"b": "TOOLONGSYMBOLNAMEEXCEEDSLIMIT", "q": "USDT", "tags": []},
        {"b": None, "q": "USDT", "tags": []},
        {"b": "OK1", "q": "BTC", "tags": []},  # wrong quote — dropped
        {"b": "OK2", "q": "USDT", "tags": ["Monitoring"]},
    ]}
    out = asset_guard.parse_products(payload)
    assert list(out.keys()) == ["OK2"]


def test_oversized_response_content_length_header_raises(monkeypatch):
    """Rejected using the Content-Length header alone — before any body is read."""
    resp = _FakeResp(b"{}", headers={"content-length": str(asset_guard._MAX_RESPONSE_BYTES + 1)})
    monkeypatch.setattr(asset_guard.httpx, "stream", lambda method, url, **kw: resp)
    with pytest.raises(asset_guard.FetchError):
        asset_guard._get_json(asset_guard._PRODUCTS_URL)


def test_oversized_streamed_body_without_content_length_raises(monkeypatch):
    """No Content-Length header (or a chunked response) still gets caught by the running total."""
    big = b"x" * (asset_guard._MAX_RESPONSE_BYTES + 1)
    resp = _FakeResp(big)
    monkeypatch.setattr(asset_guard.httpx, "stream", lambda method, url, **kw: resp)
    with pytest.raises(asset_guard.FetchError):
        asset_guard._get_json(asset_guard._PRODUCTS_URL)


def test_overall_deadline_exceeded_raises(monkeypatch):
    """Item 5: a slow trickle must not run longer than _TOTAL_TIMEOUT_SEC overall, even though
    each individual chunk arrives well within httpx's own per-read timeout."""
    class _SlowResp(_FakeResp):
        def iter_bytes(self):
            yield b"a"
            yield b"b"
            yield b"c"

    resp = _SlowResp(b"")
    monkeypatch.setattr(asset_guard.httpx, "stream", lambda method, url, **kw: resp)
    times = iter([0.0, 0.0, 5.0, 25.0])  # start, then one check per chunk (3rd exceeds 20s)
    monkeypatch.setattr(asset_guard.time, "monotonic", lambda: next(times))
    with pytest.raises(asset_guard.FetchError, match="deadline"):
        asset_guard._get_json(asset_guard._PRODUCTS_URL)


def test_non_json_response_raises_fetch_error(monkeypatch):
    resp = _FakeResp(b"not valid json {")
    monkeypatch.setattr(asset_guard.httpx, "stream", lambda method, url, **kw: resp)
    with pytest.raises(asset_guard.FetchError):
        asset_guard._get_json(asset_guard._PRODUCTS_URL)


def test_transport_failure_raises_fetch_error(monkeypatch):
    resp = _FakeResp(b"{}", status_ok=False)
    monkeypatch.setattr(asset_guard.httpx, "stream", lambda method, url, **kw: resp)
    with pytest.raises(asset_guard.FetchError):
        asset_guard._get_json(asset_guard._PRODUCTS_URL)


# --- delisting article parsing (best-effort) --------------------------------------------


def test_extract_delisted_bases_explicit_list():
    title = "Binance Will Delist ICX, SCRT, STORJ on 2026-09-03"
    assert asset_guard.extract_delisted_bases(title) == {"ICX", "SCRT", "STORJ"}


def test_extract_delisted_bases_pair_mentions():
    body = "Binance will remove the following spot trading pairs: BREV/USDT, COOKIE/USDT"
    assert asset_guard.extract_delisted_bases(body) == {"BREV", "COOKIE"}


def test_extract_delisted_bases_generic_title_no_pairs():
    title = "Notice of Removal of Spot Trading Pairs - 2026-09-18"
    assert asset_guard.extract_delisted_bases(title) == set()


def test_extract_delisted_bases_ampersand_separator():
    title = "Binance Will Delist BTTC & POWR on 2026-08-14"
    assert asset_guard.extract_delisted_bases(title) == {"BTTC", "POWR"}


def test_extract_delisted_bases_name_annotation_form():
    # "^[A-Z0-9]{2,20}$" is the shared symbol-shape floor (module-wide, ≥2 chars) — a real
    # single-letter Alpha ticker like "U" is out of scope for this module by that same rule.
    title = "Binance Will Delist XRQ (Real Union) on 2026-09-01"
    bases = asset_guard.extract_delisted_bases(title)
    assert bases == {"XRQ"}
    assert "REAL" not in bases and "UNION" not in bases


def test_extract_pair_mentions_only_matches_bot_quote():
    text = "The following pairs will be removed: QNT/USDC, OPEN/FDUSD, BREV/USDT"
    assert asset_guard.extract_pair_mentions(text, quote="USDT") == {"BREV"}
    assert asset_guard.extract_pair_mentions(text, quote="USDC") == {"QNT"}


def test_extract_pair_mentions_is_the_only_body_safe_pattern():
    assert asset_guard.extract_pair_mentions("BREV/USDT, COOKIE/USDT") == {"BREV", "COOKIE"}
    # The list pattern is NOT part of extract_pair_mentions.
    assert asset_guard.extract_pair_mentions("Delist AAA, BBB on 2026-01-01") == set()


@pytest.mark.parametrize("title", [
    "Binance Margin Will Delist BTTC & POWR on 2026-08-14",
    "Binance Margin And Loan Will Delist BTTC & POWR on 2026-08-14",
    "Binance Simple Earn Will Delist XYZ on 2026-09-01",
    "Binance Futures Will Delist ABCUSD_PERP Contracts on 2026-09-01",
    "Binance Alpha Will Remove MTP, BDXN on 2026-09-01",
    "Binance Convert Will Delist ZZZ on 2026-09-01",
    "Binance Loan Will Delist ZZZ as Collateral on 2026-09-01",
])
def test_non_spot_titles_are_excluded(title):
    assert not asset_guard._is_spot_scope_title(title)


def test_spot_titles_still_count():
    assert asset_guard._is_spot_scope_title("Binance Will Delist ICX, SCRT, STORJ on 2026-09-03")
    assert asset_guard._is_spot_scope_title("Notice of Removal of Spot Trading Pairs - 2026-09-18")


def test_fetch_delisted_bases_end_to_end(monkeypatch):
    articles_payload = {"data": {"articles": [
        {"title": "Binance Will Delist ICX, SCRT, STORJ on 2026-09-03", "code": "abc123"},
        {"title": "Some unrelated announcement", "code": "zzz"},
    ]}}
    detail_payload = {"data": {"title": "Notice", "body": "BREV/USDT, COOKIE/USDT will be removed"}}

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._ARTICLE_LIST_URL:
            return _json_resp(articles_payload)
        if url == asset_guard._ARTICLE_DETAIL_URL:
            return _json_resp(detail_payload)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    bases = asset_guard.fetch_delisted_bases()
    assert {"ICX", "SCRT", "STORJ", "BREV", "COOKIE"} <= bases


def test_fetch_delisted_bases_handles_live_catalogs_shape(monkeypatch):
    """Real shape (verified 2026-09-21): data.catalogs[*].articles, not data.articles."""
    articles_payload = {"data": {"catalogs": [
        {"catalogId": 161, "articles": [
            {"title": "Binance Will Delist ACX, HFT, PIVX on 2026-08-17", "code": "c1"},
        ]},
    ]}}
    detail_payload = {"data": {"body": "no pairs here"}}

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._ARTICLE_LIST_URL:
            return _json_resp(articles_payload)
        return _json_resp(detail_payload)

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    bases = asset_guard.fetch_delisted_bases()
    assert {"ACX", "HFT", "PIVX"} <= bases


def test_fetch_delisted_bases_skips_non_spot_titles(monkeypatch):
    articles_payload = {"data": {"articles": [
        {"title": "Binance Margin Will Delist BTTC & POWR on 2026-08-14", "code": "c1"},
        {"title": "Binance Will Delist ICX on 2026-09-03", "code": "c2"},
    ]}}

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._ARTICLE_LIST_URL:
            return _json_resp(articles_payload)
        return _json_resp({"data": {"body": ""}})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    bases = asset_guard.fetch_delisted_bases()
    assert bases == {"ICX"}
    assert "BTTC" not in bases and "POWR" not in bases


def test_fetch_delisted_bases_only_blocks_own_quote_removals(monkeypatch):
    """Regression (verified live 2026-09-21): a 'Notice of Removal of Spot Trading Pairs'
    commonly removes SPECIFIC pairs (QNT/USDC, OPEN/FDUSD) while the base keeps trading vs
    USDT — BREV, LA, QNT, OPEN, SAGA were all named in such a notice yet all still TRADING vs
    USDT. Only a pair naming the bot's own quote (USDT) should block."""
    articles_payload = {"data": {"articles": [
        {"title": "Notice of Removal of Spot Trading Pairs - 2026-09-18", "code": "c1"},
    ]}}
    body = "The following pairs will be removed: QNT/USDC, OPEN/FDUSD, and BREV/USDT."

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._ARTICLE_LIST_URL:
            return _json_resp(articles_payload)
        return _json_resp({"data": {"body": body}})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    bases = asset_guard.fetch_delisted_bases()
    assert bases == {"BREV"}
    assert "QNT" not in bases and "OPEN" not in bases


def test_body_boilerplate_never_leaks_through_list_pattern(monkeypatch):
    """Regression lock (found via a real fetch 2026-09-21): every Binance delisting detail page
    repeats generic template prose ("...delist and cease trading on...", "...delisting schedule
    may or may not apply to the products listed below...") that the title-only list pattern
    would wrongly match on BODY text, minting bogus bases out of ordinary English words
    ('AND', 'THE', 'PAIRS', ...). Body text must go through pair-mentions ONLY."""
    articles_payload = {"data": {"articles": [
        {"title": "Binance Will Delist REALCOIN on 2026-09-01", "code": "c1"},
    ]}}
    boilerplate_body = (
        "This is a general notice. Binance may delist and cease trading on any asset. "
        "The delisting schedule may or may not apply to the products listed below, "
        "depending on market conditions. Binance will delist the aforementioned spot "
        "trading pairs on the effective date shown above. REALPAIR/USDT is affected."
    )

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._ARTICLE_LIST_URL:
            return _json_resp(articles_payload)
        return _json_resp({"data": {"body": boilerplate_body}})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    bases = asset_guard.fetch_delisted_bases()
    assert bases == {"REALCOIN", "REALPAIR"}
    for noise in ("AND", "THE", "PAIRS", "CEASE", "SCHEDULE", "PRODUCTS", "LISTED", "APPLY"):
        assert noise not in bases


def test_fetch_delisted_bases_returns_none_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(asset_guard.httpx, "stream",
                        lambda method, url, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    assert asset_guard.fetch_delisted_bases() is None


def test_fetch_delisted_bases_returns_none_on_empty_article_list(monkeypatch):
    def _fake_stream(method, url, params=None, timeout=None):
        return _json_resp({"data": {"articles": []}})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    assert asset_guard.fetch_delisted_bases() is None


def test_fetch_delisted_bases_returns_empty_set_when_nothing_matches(monkeypatch):
    """A NORMAL, trustworthy 'nothing new' result: articles WERE fetched, just none matched —
    this legitimately replaces the previous list (old delistings roll off the lookback)."""
    def _fake_stream(method, url, params=None, timeout=None):
        return _json_resp({"data": {"articles": [{"title": "unrelated news", "code": "x"}]}})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    assert asset_guard.fetch_delisted_bases() == set()


def test_refresh_carries_forward_delisted_list_when_article_fetch_fails(db, monkeypatch):
    """Round-2 item 2: an article-fetch failure/empty list must not silently erase a
    previously-known delisting — the products half of the snapshot can still refresh."""
    _seed_fresh_snapshot(db, delisted={"ICX", "SCRT"})

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._PRODUCTS_URL:
            return _json_resp(_SAMPLE_PRODUCTS)
        raise RuntimeError("article endpoint down")

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    asset_guard.refresh_if_due(db)

    snap = asset_guard._load_snapshot(db)
    assert set(snap["delisted"]) == {"ICX", "SCRT"}  # carried forward, not erased
    assert db.query(AuditLog).filter_by(action="asset_guard_delist_fetch_failed").count() == 1


def test_refresh_delist_fail_audit_is_once_per_episode(db, monkeypatch):
    _seed_fresh_snapshot(db, delisted={"ICX"})

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._PRODUCTS_URL:
            return _json_resp(_SAMPLE_PRODUCTS)
        raise RuntimeError("article endpoint down")

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    monkeypatch.setattr(settings, "asset_guard_refresh_min", 0)
    asset_guard.refresh_if_due(db)
    asset_guard.refresh_if_due(db)
    asset_guard.refresh_if_due(db)
    assert db.query(AuditLog).filter_by(action="asset_guard_delist_fetch_failed").count() == 1

    # Recovery: article fetch succeeds again (and still finds something) -> episode clears ->
    # a LATER failure has something to lose again, so it re-audits.
    def _fake_stream_recovered(method, url, params=None, timeout=None):
        if url == asset_guard._PRODUCTS_URL:
            return _json_resp(_SAMPLE_PRODUCTS)
        return _json_resp({"data": {"articles": [
            {"title": "Binance Will Delist ICX on 2026-09-05", "code": "x"},
        ]}})

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream_recovered)
    asset_guard.refresh_if_due(db)
    from app import runtime

    assert runtime.get(db, asset_guard.RUNTIME_KEY_DELIST_FAIL_EPISODE) == ""

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    asset_guard.refresh_if_due(db)
    assert db.query(AuditLog).filter_by(action="asset_guard_delist_fetch_failed").count() == 2


def test_refresh_no_carry_forward_audit_when_nothing_to_lose(db, monkeypatch):
    """No previous delisted bases -> nothing lost -> no alert needed."""
    _seed_fresh_snapshot(db, delisted=set())

    def _fake_stream(method, url, params=None, timeout=None):
        if url == asset_guard._PRODUCTS_URL:
            return _json_resp(_SAMPLE_PRODUCTS)
        raise RuntimeError("article endpoint down")

    monkeypatch.setattr(asset_guard.httpx, "stream", _fake_stream)
    asset_guard.refresh_if_due(db)
    assert db.query(AuditLog).filter_by(action="asset_guard_delist_fetch_failed").count() == 0


# --- universe / scanner wiring -----------------------------------------------------------


def test_trade_block_reason_blocks_flagged_symbol(db):
    from app import scanner

    _seed_fresh_snapshot(db)
    reason = scanner._trade_block_reason(db, "AVA")
    assert reason is not None and "monitoring" in reason
    assert db.query(AuditLog).filter_by(action="skipped_asset_guard").count() == 1


def test_trade_block_reason_allows_clean_symbol(db):
    from app import scanner

    _seed_fresh_snapshot(db)
    assert scanner._trade_block_reason(db, "BTC") is None


def test_review_and_open_refuses_flagged_symbol_defense_in_depth(db, monkeypatch):
    """Targets the second (pre-open) check inside ``_review_and_open`` directly — bypasses
    the earlier ``_trade_block_reason`` pre-check to prove THIS check independently refuses."""
    from app import scanner
    from app.models import Candidate, ScanRun

    _seed_fresh_snapshot(db)
    monkeypatch.setattr(settings, "watchlist", [])
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, force=False: {"AVA": 10.0})
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: {"AVA": 10.0})
    from app.orchestrator import grok
    monkeypatch.setattr(grok, "scanner_enabled", lambda: False)

    scan = ScanRun(mode="semi", universe_size=1, params="{}")
    db.add(scan)
    db.flush()
    cand = Candidate(scan_id=scan.id, symbol="AVA", consensus_pct=90.0, win_rate=90.0,
                      win_rate_lb=90.0, expectancy=5.0, trials=20, decision="trade")
    db.add(cand)
    db.flush()
    to_open = [{
        "cand": cand, "symbol": "AVA", "entry": 10.0, "distance_pct": 2.0, "tp_pct": 3.0,
        "max_waves": 5, "strategy_mode": "dca_down",
    }]
    scanner._review_and_open(db, to_open, "semi")
    db.commit()

    assert cand.session_id is None
    assert "chặn: monitoring" in (cand.reason or "")
    assert db.query(AuditLog).filter_by(action="skipped_asset_guard").count() == 1


def test_universe_filters_blocked_before_max_symbols_cut(db, monkeypatch):
    """Fix: the guard must run BEFORE the scan_max_symbols cut — filtering after it would let a
    blocked coin occupy a slot forever, crowding out a healthy coin further down the ranking."""
    from app import scanner

    _seed_fresh_snapshot(db)  # AVA (monitoring), NVDAB (stock_token) blocked; BTC/ETH clean
    monkeypatch.setattr(settings, "watchlist", [])
    monkeypatch.setattr(settings, "scan_max_symbols", 2)

    class _FakeProvider:
        def all_symbols(self, min_quote_volume=0.0):
            # Blocked symbols rank FIRST by "volume" — a naive cut-then-filter would return an
            # empty (or short) universe and never reach BTC/ETH.
            return ["AVA", "NVDAB", "BTC", "ETH"]

    universe = scanner._universe(db, _FakeProvider())
    assert universe == ["BTC", "ETH"]


# --- active session unaffected + one dedup'd alert ----------------------------------------


def _active_session(db, symbol="AVA"):
    row = KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=6,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=43200.0, gap_y_min=0.0,
        status=SESSION_ACTIVE, current_wave=1, avg_price=100.0,
        total_filled_qty=10.0, total_cost=1000.0, sl_pct=8.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_active_session_rung_and_tp_unaffected_by_guard(db, monkeypatch):
    from app.kss import service

    _seed_fresh_snapshot(db)  # AVA is classified "monitoring"
    assert asset_guard.blocked_reason(db, "AVA") == "monitoring"

    row = _active_session(db)

    # Rung: a manual DCA+ still queues normally on a flagged coin.
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, force=False: {"AVA": 95.0})
    pending_before = db.query(PendingOrder).count()
    service.queue_next_wave(db, row.id)
    db.commit()
    assert db.query(PendingOrder).count() == pending_before + 1
    assert db.query(KssWave).filter_by(session_id=row.id, wave_num=2).count() == 1

    # Exit: take-profit still queues normally on a flagged coin (price clears avg×(1+tp%)).
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, force=False: {"AVA": 110.0})
    triggered = service.manage_open_sessions(db)
    db.commit()
    assert row.id in triggered
    sell = (db.query(PendingOrder)
            .filter_by(symbol="AVA", side="SELL").order_by(PendingOrder.id.desc()).first())
    assert sell is not None


def test_audit_held_symbols_alerts_once_then_dedups(db, monkeypatch):
    from app import notify

    _seed_fresh_snapshot(db)
    _active_session(db, symbol="AVA")
    sent = []
    monkeypatch.setattr(notify, "event", lambda kind, text, **kw: sent.append((kind, text)) or True)

    fired = asset_guard.audit_held_symbols(db)
    assert fired == ["AVA"]
    assert len(sent) == 1
    assert db.query(AuditLog).filter_by(action="held_symbol_flagged", entity="AVA").count() == 1

    # Second call: same reason still active -> deduped, no new alert/audit row.
    fired2 = asset_guard.audit_held_symbols(db)
    assert fired2 == []
    assert len(sent) == 1
    assert db.query(AuditLog).filter_by(action="held_symbol_flagged", entity="AVA").count() == 1


def test_audit_held_symbols_aggregates_into_one_message(db, monkeypatch):
    from app import notify

    _seed_fresh_snapshot(db)
    _active_session(db, symbol="AVA")
    _active_session(db, symbol="PAXG")
    sent = []
    monkeypatch.setattr(notify, "event", lambda kind, text, **kw: sent.append((kind, text)) or True)

    fired = asset_guard.audit_held_symbols(db)
    assert set(fired) == {"AVA", "PAXG"}
    assert len(sent) == 1  # ONE aggregated message, not one per symbol
    assert "AVA" in sent[0][1] and "PAXG" in sent[0][1]
    assert db.query(AuditLog).filter_by(action="held_symbol_flagged").count() == 2


def test_audit_held_symbols_clears_dedupe_when_flag_disappears(db, monkeypatch):
    from app import notify, runtime

    _seed_fresh_snapshot(db)
    _active_session(db, symbol="AVA")
    monkeypatch.setattr(notify, "event", lambda *a, **kw: True)

    asset_guard.audit_held_symbols(db)
    assert runtime.get(db, "asset_guard_flagged:AVA") == "monitoring"

    # Flag clears (e.g. monitoring knob turned off) — dedupe key must reset, not just go stale.
    monkeypatch.setattr(settings, "asset_guard_block_monitoring", False)
    asset_guard.audit_held_symbols(db)
    assert runtime.get(db, "asset_guard_flagged:AVA") == ""

    # Flag re-appears with the SAME reason — must alert again (a fresh episode).
    monkeypatch.setattr(settings, "asset_guard_block_monitoring", True)
    sent = []
    monkeypatch.setattr(notify, "event", lambda kind, text, **kw: sent.append(text) or True)
    fired = asset_guard.audit_held_symbols(db)
    assert fired == ["AVA"]
    assert len(sent) == 1


def test_audit_held_symbols_does_not_clear_dedupe_while_snapshot_stale(db, monkeypatch):
    """Round-2 item 4: while the snapshot is missing/stale, blocked_reason fails open (returns
    None) for tag-based classes too — that is 'unknown', not 'cleared'. Clearing on it would
    un-dedupe every previously-flagged symbol for the whole outage, then mass re-alert the
    instant the snapshot recovers, even for symbols that were NEVER actually un-flagged."""
    from app import notify, runtime

    _seed_fresh_snapshot(db)
    _active_session(db, symbol="AVA")
    monkeypatch.setattr(notify, "event", lambda *a, **kw: True)
    asset_guard.audit_held_symbols(db)
    assert runtime.get(db, "asset_guard_flagged:AVA") == "monitoring"

    # Snapshot goes stale — blocked_reason now fails open for AVA (returns None) even though
    # nothing actually changed.
    old = asset_guard.utcnow() - timedelta(hours=200)
    stale_snap = {"fetched_at": old.isoformat(),
                  "products": asset_guard.parse_products(_SAMPLE_PRODUCTS), "delisted": []}
    asset_guard._save_snapshot(db, stale_snap)
    assert asset_guard.blocked_reason(db, "AVA") is None  # confirms the fail-open precondition

    fired = asset_guard.audit_held_symbols(db)
    assert fired == []
    assert runtime.get(db, "asset_guard_flagged:AVA") == "monitoring"  # NOT cleared while stale

    # Recovery: a fresh, trustworthy snapshot STILL shows AVA flagged -> must NOT re-alert (the
    # dedupe key survived the outage untouched).
    sent = []
    monkeypatch.setattr(notify, "event", lambda kind, text, **kw: sent.append(text) or True)
    _seed_fresh_snapshot(db)
    fired = asset_guard.audit_held_symbols(db)
    assert fired == []
    assert len(sent) == 0


def test_audit_held_symbols_noop_when_disabled(db, monkeypatch):
    _seed_fresh_snapshot(db)
    _active_session(db, symbol="AVA")
    monkeypatch.setattr(settings, "asset_guard_enabled", False)
    assert asset_guard.audit_held_symbols(db) == []


# --- manual create + OPUS rescue: refused (entry paths, never an exit path) ------------------


def test_manual_kss_create_refuses_flagged_symbol(db):
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    _seed_fresh_snapshot(db)
    with TestClient(fastapi_app) as c:
        r = c.post("/api/kss/sessions", json={
            "symbol": "AVA", "entry_price": 10.0, "distance_pct": 2,
            "max_waves": 3, "isolated_fund": 100.0, "tp_pct": 3,
        })
    assert r.status_code == 400
    assert "monitoring" in r.json()["detail"]
    assert db.query(KssSession).filter_by(symbol="AVA").count() == 0


def test_manual_kss_create_allows_clean_symbol(db):
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    _seed_fresh_snapshot(db)
    with TestClient(fastapi_app) as c:
        r = c.post("/api/kss/sessions", json={
            "symbol": "BTC", "entry_price": 50000, "distance_pct": 2,
            "max_waves": 3, "isolated_fund": 100000, "tp_pct": 3,
        })
    assert r.status_code == 200


def test_adopt_position_into_kss_still_rescues_flagged_symbol(db):
    """Round-2: a rescue of an ALREADY-HELD position is PROTECTION (attaches SL/TP/deadline to
    capital already at risk), not a new entry — refusing it would strand a losing position with
    NO exit (only RIDE has a hard stop). The guard must never do that; it belongs on the OPUS
    BUY intent instead (see test_opus_buy_refuses_flagged_symbol)."""
    from app.kss import service

    _seed_fresh_snapshot(db)  # AVA classified "monitoring"
    row = service.adopt_position_into_kss(db, "AVA", held_qty=10.0, avg_price=1.0, current_price=1.0)
    assert row.status == SESSION_ACTIVE
    assert db.query(KssSession).filter_by(symbol="AVA").count() == 1


def test_opus_rescue_of_existing_position_on_flagged_coin_still_adopts(db, monkeypatch):
    """The OPUS 3h watch loop's rescue of a LOSING position must proceed exactly as before on a
    flagged coin — see the note above ``adopt_position_into_kss``."""
    from app import market
    from app.orchestrator import models as om
    from app.orchestrator import watch

    _seed_fresh_snapshot(db)  # AVA classified "monitoring"
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"AVA": 1.0})
    pos = om.OpusPosition(
        symbol="AVA", state=om.OPUS_WATCH, qty=10.0, avg_price=2.0, entry_price=2.0,
        opened_at=asset_guard.utcnow() - timedelta(hours=4),
        watch_started_at=asset_guard.utcnow() - timedelta(hours=4),
    )
    db.add(pos)
    db.commit()

    summary = watch.run(db)  # price 1.0 < avg 2.0 -> loser -> rescue
    db.refresh(pos)

    assert summary["rescues"] == 1
    assert pos.state == om.OPUS_RESCUE
    assert pos.kss_session_id is not None
    sess = db.get(KssSession, pos.kss_session_id)
    assert sess is not None and sess.status == SESSION_ACTIVE


def test_opus_buy_refuses_flagged_symbol(db, monkeypatch):
    """The NEW-risk path: an OPUS BUY intent commits fresh capital, so this — not the rescue —
    is where the entry guard belongs."""
    from app import market
    from app.orchestrator import brain, policy
    from app.orchestrator.models import OpusPosition

    _seed_fresh_snapshot(db)  # AVA classified "monitoring"
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"AVA": 1.0})
    monkeypatch.setattr(
        brain, "_candidates",
        lambda db, k=25: [{"symbol": "AVA", "decision": "trade", "consensus": 80,
                           "win_rate": 90, "est_days_to_tp": 3}],
    )
    monkeypatch.setattr(settings, "opus_shadow", False)
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(settings, "opus_max_trade_notional", 200.0)

    out = policy.apply_intents(
        db, [{"action": "open", "symbol": "AVA", "notional": 100, "reason": "x"}]
    )
    assert out["executed"] == []
    assert out["rejected"][0]["reason"] == "asset_guard:monitoring"
    assert db.query(OpusPosition).count() == 0
    assert db.query(AuditLog).filter_by(
        action="open_asset_guard_blocked", entity="AVA"
    ).count() == 1


def test_opus_buy_allows_clean_symbol(db, monkeypatch):
    from app import market
    from app.orchestrator import brain, policy
    from app.orchestrator.models import OpusPosition

    _seed_fresh_snapshot(db)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"BTC": 100.0})
    monkeypatch.setattr(
        brain, "_candidates",
        lambda db, k=25: [{"symbol": "BTC", "decision": "trade", "consensus": 80,
                           "win_rate": 90, "est_days_to_tp": 3}],
    )
    monkeypatch.setattr(settings, "opus_shadow", False)
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(settings, "opus_max_trade_notional", 200.0)

    out = policy.apply_intents(
        db, [{"action": "open", "symbol": "BTC", "notional": 100, "reason": "x"}]
    )
    assert len(out["executed"]) == 1
    assert db.query(OpusPosition).count() == 1


# --- blocked_symbols_by_reason (UI transparency table) --------------------------------------


def test_blocked_symbols_by_reason_groups_and_dedupes(db, monkeypatch):
    monkeypatch.setattr(settings, "asset_guard_denylist", "FTT")
    _seed_fresh_snapshot(db, delisted={"ICX"})
    out = asset_guard.blocked_symbols_by_reason(db)
    assert out["monitoring"] == ["AVA"]
    assert out["stock_token"] == ["NVDAB"]
    assert out["commodity"] == ["PAXG"]
    assert "WBTC" in out["wrapped"] and "WBETH" in out["wrapped"] and "BETH" in out["wrapped"]
    assert out["stablecoin"] == ["RLUSD"]
    assert out["delist_announced"] == ["ICX"]
    assert out["denylist"] == ["FTT"]
