"""
Tests for scripts/research_dataset.py — the offline archive loader behind every
cross-sectional study.

WHY this file exists: each helper below encodes a documented trap in Binance's public
archive, and every one of them fails SILENTLY in a study rather than raising.

  - Spot timestamps switched from milliseconds to MICROSECONDS on 2025-01-01, and both eras
    appear inside one symbol's history. Unnormalised, a 2025 bar lands ~52,000 years in the
    future and every forward-return join quietly drops it.
  - Newer archive files carry a CSV header row, older ones do not. A header parsed as data
    becomes a garbage bar at epoch 0.
  - Leveraged tokens (…UP/…DOWN/…BULL/…BEAR) decay by construction; in a cross-sectional
    ranking they look like coins with a permanent negative drift.
  - The whole point of using the archive is that DELISTED pairs are still in it, so the
    symbol filter must not reject anything merely for being dead.

No network, no DB: only the pure functions are exercised here.
"""

from __future__ import annotations

from scripts.research_dataset import (
    checksum_matches,
    is_study_symbol,
    month_range,
    months_from_listing,
    normalize_ts,
    parse_kline_csv,
    parse_metrics_csv,
)

# A real 2024 row (milliseconds) and a real 2025 row (microseconds), same shape.
ROW_MS = "1704067200000,42283.58,42554.57,42261.02,42475.23,1271.35,1704153599999,53956000.1,52341,600.1,25000.0,0"
ROW_US = "1735689600000000,93429.99,94903.00,93129.99,94419.76,1044.98,1735775999999999,98218000.5,44120,500.0,47000.0,0"


class TestNormalizeTs:
    def test_milliseconds_pass_through(self):
        assert normalize_ts("1704067200000") == 1704067200000

    def test_microseconds_are_scaled_down(self):
        # 2025-01-01 in the archive's post-switch unit must land on the same instant in ms.
        assert normalize_ts("1735689600000000") == 1735689600000

    def test_the_two_eras_agree_on_ordering(self):
        # The failure this prevents: an unnormalised 2025 bar sorts after the year 54000.
        assert normalize_ts(ROW_MS.split(",")[0]) < normalize_ts(ROW_US.split(",")[0])

    def test_accepts_int_as_well_as_str(self):
        assert normalize_ts(1704067200000) == 1704067200000


class TestIsStudySymbol:
    def test_ordinary_pair_is_kept(self):
        assert is_study_symbol("SOLUSDT")

    def test_delisted_pair_is_kept(self):
        # Survivorship-freedom is the reason we use the archive at all.
        assert is_study_symbol("SRMUSDT")

    def test_leveraged_tokens_are_dropped(self):
        for sym in ("1INCHUPUSDT", "1INCHDOWNUSDT", "ETHBULLUSDT", "ETHBEARUSDT"):
            assert not is_study_symbol(sym), sym

    def test_stable_and_fiat_bases_are_dropped(self):
        for sym in ("USDCUSDT", "BUSDUSDT", "FDUSDUSDT", "EURUSDT", "TRYUSDT"):
            assert not is_study_symbol(sym), sym

    def test_other_quotes_are_dropped(self):
        assert not is_study_symbol("ETHBTC")
        assert not is_study_symbol("SOLBTC")

    def test_a_coin_whose_name_ends_in_up_is_not_leveraged(self):
        # The suffix rule applies to the BASE, and only as a whole-token suffix.
        assert is_study_symbol("SUPERUSDT")
        assert is_study_symbol("JUPUSDT")


class TestMonthRange:
    def test_inclusive_both_ends(self):
        assert month_range("2024-11", "2025-02") == ["2024-11", "2024-12", "2025-01", "2025-02"]

    def test_single_month(self):
        assert month_range("2026-09", "2026-09") == ["2026-09"]

    def test_empty_when_reversed(self):
        assert month_range("2026-09", "2026-08") == []


class TestParseKlineCsv:
    def test_parses_both_eras_into_milliseconds(self):
        rows = parse_kline_csv(f"{ROW_MS}\n{ROW_US}")
        assert len(rows) == 2
        assert rows[0][0] == 1704067200000
        assert rows[1][0] == 1735689600000

    def test_header_row_is_skipped_not_stored_as_a_bar(self):
        header = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,"
                  "taker_buy_volume,taker_buy_quote_volume,ignore")
        rows = parse_kline_csv(f"{header}\n{ROW_MS}")
        assert len(rows) == 1
        assert rows[0][0] == 1704067200000

    def test_fields_land_in_the_documented_order(self):
        (ts, o, h, low, c, vol, qvol, trades), = parse_kline_csv(ROW_MS)
        assert (o, h, low, c) == (42283.58, 42554.57, 42261.02, 42475.23)
        assert vol == 1271.35 and qvol == 53956000.1 and trades == 52341

    def test_a_damaged_row_does_not_lose_the_month(self):
        rows = parse_kline_csv(f"{ROW_MS}\nnot,a,real,row\n{ROW_US}")
        assert len(rows) == 2

    def test_blank_and_short_lines_ignored(self):
        assert parse_kline_csv("\n\n1,2,3\n") == []


class TestParseMetricsCsv:
    LINE = ("2020-09-01 00:00:00,BTCUSDT,39080.23100000,456144339.23360443,"
            "1.17547937,1.23012681,1.35731217,0.78373373")

    def test_parses_a_real_row(self):
        (ts, oi, oiv, top_acct, top_pos, glob, taker), = parse_metrics_csv(self.LINE)
        assert ts == 1598918400000                      # 2020-09-01T00:00:00Z
        assert oi == 39080.231 and oiv == 456144339.23360443
        assert (top_acct, top_pos, glob, taker) == (1.17547937, 1.23012681, 1.35731217, 0.78373373)

    def test_header_is_skipped(self):
        header = ("create_time,symbol,sum_open_interest,sum_open_interest_value,"
                  "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
                  "count_long_short_ratio,sum_taker_long_short_vol_ratio")
        assert parse_metrics_csv(f"{header}\n{self.LINE}") == parse_metrics_csv(self.LINE)


class TestMonthsFromListing:
    XML = (
        "<ListBucketResult>"
        "<Contents><Key>data/spot/monthly/klines/SOLUSDT/1d/SOLUSDT-1d-2024-11.zip</Key></Contents>"
        "<Contents><Key>data/spot/monthly/klines/SOLUSDT/1d/SOLUSDT-1d-2024-11.zip.CHECKSUM</Key></Contents>"
        "<Contents><Key>data/spot/monthly/klines/SOLUSDT/1d/SOLUSDT-1d-2024-12.zip</Key></Contents>"
        "</ListBucketResult>"
    )

    def test_returns_months_once_each(self):
        # The .CHECKSUM sibling must not double every month.
        assert months_from_listing(self.XML, "SOLUSDT", "1d") == ["2024-11", "2024-12"]

    def test_does_not_match_another_symbol_or_interval(self):
        assert months_from_listing(self.XML, "SOLUSDT", "4h") == []
        assert months_from_listing(self.XML, "ETHUSDT", "1d") == []


class TestChecksum:
    def test_matches_the_sibling_file_format(self):
        import hashlib
        payload = b"binance"
        digest = hashlib.sha256(payload).hexdigest()
        assert checksum_matches(payload, f"{digest}  BTCUSDT-1d-2024-01.zip")

    def test_rejects_a_reissued_file(self):
        # Binance silently reissues corrected archives; a stale cache must be detected.
        assert not checksum_matches(b"new content", "0" * 64 + "  BTCUSDT-1d-2024-01.zip")
