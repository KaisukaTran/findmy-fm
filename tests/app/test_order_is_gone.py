"""Fix round A / item 5(c): `order_is_gone` must never match a bare substring.

Binance answers -2011 ("Unknown order sent") for a cancel on an order the venue no longer
holds, and -2013 ("Order does not exist") for the same case read via `fetch_order` — both mean
"nothing here to fail on, read the final status instead". The old check matched `"-2011" in
str(exc)`: a NETWORK error (not a real -2011 response) whose message embeds the signed request
URL — which carries `origClientOrderId=fm-<order id>` — would false-positive the moment an
order id reaches 2011 (imminent; ids are in the low thousands), short-circuiting BEFORE
`note_rate_error`/`note_credential_error` ever see the error. Re-keyed on the ccxt exception
TYPE or a JSON-shaped `"code":-2011`/`"code":-2013`, exactly like `classify_credential_error`.
"""

from __future__ import annotations

import ccxt

from app import execution


def _real_2011() -> Exception:
    """Shaped like ccxt actually raises it: id + status + reason + the raw JSON body."""
    return ccxt.OrderNotFound(
        'binance {"code":-2011,"msg":"Unknown order sent."}'
    )


def _real_2013_non_order_not_found_type() -> Exception:
    """A -2013 that, hypothetically, ccxt did NOT map to OrderNotFound — the JSON-shape match
    must still catch it (defense in depth, mirrors classify_credential_error)."""
    return ccxt.ExchangeError('binance {"code":-2013,"msg":"Order does not exist."}')


def test_a_real_minus_2011_is_gone():
    assert execution.order_is_gone(_real_2011()) is True


def test_a_json_shaped_minus_2013_is_gone_even_off_a_plain_exchange_error():
    assert execution.order_is_gone(_real_2013_non_order_not_found_type()) is True


def test_a_network_error_naming_client_order_id_fm_2011_is_not_gone():
    """order id 2011 -> clientOrderId `fm-2011` -> the signed URL embeds
    `origClientOrderId=fm-2011`. A bare substring match on "-2011" would misread this network
    error as a gone order and never reach note_rate_error/note_credential_error."""
    exc = ccxt.NetworkError(
        "binance GET https://api.binance.com/api/v3/order?symbol=SOLUSDT"
        "&origClientOrderId=fm-2011&timestamp=1756500000429&signature=deadbeef read timeout"
    )
    assert execution.order_is_gone(exc) is False


def test_an_unrelated_exception_is_not_gone():
    assert execution.order_is_gone(RuntimeError("network blip")) is False
