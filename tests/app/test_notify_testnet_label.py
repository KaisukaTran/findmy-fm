"""Telegram must not call testnet "LIVE".

The label was derived from live_trading alone, so an instance trading PLAY money on Binance
testnet sent messages tagged [LIVE] — identical to what real funds would send. Ahead of a month
of testnet followed by a switch to real money, that is the one distinction the operator needs
their phone to make, and the switch itself would produce no visible change at all.
"""

from __future__ import annotations

import pytest

from app import notify
from app.config import settings


@pytest.mark.parametrize(
    ("live_trading", "testnet", "expected"),
    [
        (False, False, "paper"),
        (False, True, "paper"),     # paper is paper regardless of the testnet flag
        (True, True, "testnet"),    # real venue, play money
        (True, False, "live"),      # real money
    ],
)
def test_instance_name_separates_testnet_from_real_money(
    monkeypatch, live_trading, testnet, expected
):
    monkeypatch.setattr(settings, "live_trading", live_trading)
    monkeypatch.setattr(settings, "live_use_testnet", testnet)

    assert notify.instance_name() == expected


def test_each_instance_has_its_own_visible_tag(monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "live_use_testnet", True)
    testnet_tag = notify._label(notify.instance_name())

    monkeypatch.setattr(settings, "live_use_testnet", False)
    live_tag = notify._label(notify.instance_name())

    assert testnet_tag != live_tag, "the switch to real money must be visible in the chat"
    assert "TESTNET" in testnet_tag
    assert live_tag == "[LIVE]"


def test_testnet_is_still_routable_as_a_command_target(monkeypatch):
    """Commands are routed by instance name ('/pause live'), so the new name must be one."""
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "live_use_testnet", True)

    assert notify.instance_name() in notify._INSTANCES
