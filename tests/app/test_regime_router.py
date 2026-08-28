"""
Tests for the pure regime router (app/kss/regime.py).

Locks: router OFF -> always dca_down; a WLFI-like strong-uptrend signal ->
pyramid_up; a dip/downtrend signal -> dca_down; None trend fields don't crash.
"""

from app.kss.regime import classify_mode

_WLFI_LIKE = {
    "htf_trend": "up",
    "st_trend": "up",
    "adx": 30.0,
    "rel_strength": 5.0,
    "macdh": 0.5,
    "min_rel_strength": 1.0,
    "min_adx": 20.0,
}

_DIP_LIKE = {
    "htf_trend": "down",
    "st_trend": "down",
    "adx": 28.0,
    "rel_strength": -3.0,
    "macdh": -0.2,
    "min_rel_strength": 1.0,
    "min_adx": 20.0,
}


def test_wlfi_like_strong_uptrend_routes_to_pyramid_up():
    mode = classify_mode(enabled=True, **_WLFI_LIKE)
    assert mode == "pyramid_up"


def test_dip_downtrend_routes_to_dca_down():
    mode = classify_mode(enabled=True, **_DIP_LIKE)
    assert mode == "dca_down"


def test_router_disabled_always_dca_down_even_with_strong_signal():
    mode = classify_mode(enabled=False, **_WLFI_LIKE)
    assert mode == "dca_down"


def test_none_trend_fields_do_not_crash_and_fall_back_to_dca_down():
    mode = classify_mode(
        enabled=True,
        htf_trend=None,
        st_trend=None,
        adx=30.0,
        rel_strength=5.0,
        macdh=0.5,
        min_rel_strength=1.0,
        min_adx=20.0,
    )
    assert mode == "dca_down"


def test_only_st_trend_up_is_sufficient_for_uptrend_leg():
    mode = classify_mode(
        enabled=True,
        htf_trend=None,
        st_trend="up",
        adx=25.0,
        rel_strength=2.0,
        macdh=0.1,
        min_rel_strength=1.0,
        min_adx=20.0,
    )
    assert mode == "pyramid_up"


def test_rel_strength_at_or_below_threshold_falls_back_to_dca_down():
    mode = classify_mode(
        enabled=True,
        htf_trend="up",
        st_trend="up",
        adx=30.0,
        rel_strength=1.0,  # equal to min_rel_strength, not strictly greater
        macdh=0.5,
        min_rel_strength=1.0,
        min_adx=20.0,
    )
    assert mode == "dca_down"


def test_negative_macdh_falls_back_to_dca_down_even_in_uptrend():
    mode = classify_mode(
        enabled=True,
        htf_trend="up",
        st_trend="up",
        adx=30.0,
        rel_strength=5.0,
        macdh=-0.1,
        min_rel_strength=1.0,
        min_adx=20.0,
    )
    assert mode == "dca_down"


def test_weak_adx_falls_back_to_dca_down_even_with_other_signals_strong():
    mode = classify_mode(
        enabled=True,
        htf_trend="up",
        st_trend="up",
        adx=10.0,  # below min_adx
        rel_strength=5.0,
        macdh=0.5,
        min_rel_strength=1.0,
        min_adx=20.0,
    )
    assert mode == "dca_down"


def test_flat_trends_fall_back_to_dca_down():
    mode = classify_mode(
        enabled=True,
        htf_trend="flat",
        st_trend="flat",
        adx=30.0,
        rel_strength=5.0,
        macdh=0.5,
        min_rel_strength=1.0,
        min_adx=20.0,
    )
    assert mode == "dca_down"
