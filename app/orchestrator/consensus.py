"""
Consensus between the OPUS and Grok agents.

Default (solo_open=False) is asymmetric by design (capital-preservation): an OPEN needs
BOTH agents to agree on the symbol (slow, selective entries → fewer bad trades); a CLOSE or
REDUCE fires if EITHER agent wants it (fast risk reduction). Everything else is a hold.

P2/O-LIVE (Kai, 2026-07-16 — full solo): when `solo_open=True`, OPUS may open WITHOUT
Grok's agreement — every OPUS "open" survives (Grok, when it also proposed the same
symbol, still narrows the size to the more conservative min-notional). Grok is the
risk-skeptic role and must never open alone, so a Grok-only open is always dropped in
both modes. CLOSE stays a union regardless of `solo_open` — fast risk reduction never
waits for solo permission. The merged intents still pass through policy.py's deterministic
clamps (incl. the cage-side consensus floor, P2).

P3 (docs/opus-3pct-plan.md §2): REDUCE (partial take-profit) is risk-reduction too, unioned
by position_id exactly like CLOSE. If the SAME position ever draws both a close and a reduce
(from either agent), close wins — a full exit is the stronger of the two.
"""

from __future__ import annotations


def combine(opus_intents: list[dict], grok_intents: list[dict], *, solo_open: bool = False) -> dict:
    """Merge two agents' intents into consensus intents + a small stats dict."""
    o_open = {i["symbol"]: i for i in opus_intents if i.get("action") == "open" and i.get("symbol")}
    g_open = {i["symbol"]: i for i in grok_intents if i.get("action") == "open" and i.get("symbol")}
    agreed = set(o_open) & set(g_open)
    # solo_open: OPUS's opens all survive (Grok-only opens are dropped either way — risk
    # role never opens alone). Default: only symbols both agents proposed survive.
    open_symbols = set(o_open) if solo_open else agreed

    consensus: list[dict] = []

    for sym in sorted(open_symbols):
        if sym in agreed:
            # Both agents proposed it — keep the MORE CONSERVATIVE (min) notional and the
            # merged đồng-thuận reason, same as the AND-gate default.
            notions = [n for n in (o_open[sym].get("notional"), g_open[sym].get("notional"))
                       if isinstance(n, (int, float)) and n > 0]
            consensus.append({
                "action": "open", "symbol": sym, "position_id": None,
                "notional": min(notions) if notions else None,
                "reason": f"đồng thuận OPUS+GROK: {(o_open[sym].get('reason') or '')[:35]} | "
                          f"{(g_open[sym].get('reason') or '')[:35]}",
            })
        else:
            # solo_open only: OPUS proposed it alone — keep OPUS's own notional.
            consensus.append({
                "action": "open", "symbol": sym, "position_id": None,
                "notional": o_open[sym].get("notional"),
                "reason": f"OPUS solo: {(o_open[sym].get('reason') or '')[:55]}",
            })

    # CLOSE / REDUCE — risk-reduction union by position_id (either agent can trigger an exit
    # or a partial take-profit), unchanged by solo_open. If the same position ever draws both
    # a close and a reduce, close wins (the stronger action) regardless of arrival order.
    risk_by_pid: dict[int, dict] = {}
    for i in [*opus_intents, *grok_intents]:
        action = i.get("action")
        pid = i.get("position_id")
        if action not in {"close", "reduce"} or not isinstance(pid, int):
            continue
        current = risk_by_pid.get(pid)
        if current is None or (current["action"] != "close" and action == "close"):
            risk_by_pid[pid] = i

    n_closes = 0
    n_reduces = 0
    for pid, i in risk_by_pid.items():
        if i["action"] == "close":
            n_closes += 1
            consensus.append({
                "action": "close", "symbol": i.get("symbol"), "position_id": pid,
                "notional": None, "reason": f"close (≥1 agent): {(i.get('reason') or '')[:55]}",
            })
        else:
            n_reduces += 1
            consensus.append({
                "action": "reduce", "symbol": i.get("symbol"), "position_id": pid,
                "notional": i.get("notional"),
                "reason": f"reduce (≥1 agent): {(i.get('reason') or '')[:55]}",
            })

    stats = {
        "opus_open": len(o_open), "grok_open": len(g_open),
        "agreed_open": len(agreed), "closes": n_closes, "reduces": n_reduces,
        "solo_open": solo_open,
    }
    return {"intents": consensus, "stats": stats}
