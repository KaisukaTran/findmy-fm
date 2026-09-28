"""The `price` display filter: a sub-cent coin must not render as 0.00 (PUMP at 0.00482 did)."""

import pytest

from app.routes import _price, templates


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (0.0048234549222, "0.004823"),  # PUMP — was "0.00"
        (0.014067094032, "0.01407"),  # BABY — was "0.01"
        (0.11997041122799999, "0.1200"),
        (0.00001234567, "0.00001235"),  # PEPE-sized
        (1.8826320456, "1.8826"),
        (13.098426558623183, "13.0984"),
        (99.99991, "99.9999"),
        (150.126, "150.13"),
        (62634.53, "62,634.53"),
        (0, "0.00"),
        (None, "0.00"),
        (-0.0048234, "-0.004823"),
    ],
)
def test_price_keeps_four_significant_digits_below_one(value, shown):
    assert _price(value) == shown


def test_price_is_registered_as_a_template_filter():
    assert templates.env.filters["price"] is _price
