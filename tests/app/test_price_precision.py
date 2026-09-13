"""One price-rounding rule for the whole KSS engine, and it must not distort tiny prices.

Paper PEPE, 2026-09-13: the flat six decimals for "small alts" turned an entry of 3.47e-06
into a 3e-06 wave-0 limit (13.5% under the market) and put every rung 13% below instead of 4%.
"""

import pytest

from app.kss import dynamic_exit, pyramid_up
from app.kss.precision import price_precision
from app.kss.pyramid import PyramidSession


@pytest.mark.parametrize("price,decimals", [
    (77_172.0, 2), (12_000.0, 2), (250.0, 4), (100.0, 4),
    (35.01, 6), (0.695, 6), (0.0679, 6), (0.0123, 6),   # ≥ $0.01: exactly as before
    (0.00527, 7), (5.272e-05, 9), (5.25e-06, 10), (3.47e-06, 10), (2.81e-06, 10),
])
def test_decimals_keep_five_significant_digits_below_a_cent(price, decimals):
    assert price_precision(price) == decimals


@pytest.mark.parametrize("price", [3.47e-06, 2.81e-06, 5.25e-06, 5.272e-05, 0.0679, 0.695, 101.29, 77_172.0])
def test_rounding_never_moves_a_price_by_more_than_a_basis_point(price):
    rounded = round(price, price_precision(price))
    assert abs(rounded / price - 1) < 1e-4


def test_the_three_engine_helpers_agree():
    for p in (3.47e-06, 0.0679, 250.0, 50_000.0):
        assert dynamic_exit.price_precision(p) == pyramid_up.price_precision(p) == price_precision(p)


def test_pepe_ladder_targets_follow_the_distance_not_the_rounding():
    py = PyramidSession(symbol="PEPE", entry_price=3.47e-06, distance_pct=4.0, max_waves=10,
                        isolated_fund=3000.0, tp_pct=5.0, timeout_x_min=60, gap_y_min=5)
    for n in range(10):
        raw = 3.47e-06 * (0.96 ** n)
        assert abs(py.generate_wave(n).target_price / raw - 1) < 1e-4, n
    assert py.generate_wave(0).target_price == pytest.approx(3.47e-06)
    assert py.generate_wave(1).target_price == pytest.approx(3.47e-06 * 0.96, rel=1e-4)


def test_a_normal_alt_rounds_exactly_as_before():
    py = PyramidSession(symbol="THE", entry_price=0.0679, distance_pct=4.0, max_waves=3,
                        isolated_fund=300.0, tp_pct=5.0, timeout_x_min=60, gap_y_min=5)
    assert py.generate_wave(1).target_price == round(0.0679 * 0.96, 6)
