"""
Consensus between the OPUS and Grok agents.

Asymmetric by design (capital-preservation): an OPEN needs BOTH agents to agree on the
symbol (slow, selective entries → fewer bad trades); a CLOSE fires if EITHER agent wants
out (fast risk reduction). Everything else is a hold. The merged intents still pass through
policy.py's deterministic clamps.
"""

from __future__ import annotations


def combine(opus_intents: list[dict], grok_intents: list[dict]) -> dict:
    """Merge two agents' intents into consensus intents + a small stats dict."""
    o_open = {i["symbol"]: i for i in opus_intents if i.get("action") == "open" and i.get("symbol")}
    g_open = {i["symbol"]: i for i in grok_intents if i.get("action") == "open" and i.get("symbol")}

    consensus: list[dict] = []

    # OPEN — only symbols BOTH agents proposed; size at the MORE CONSERVATIVE (min) notional.
    for sym in sorted(set(o_open) & set(g_open)):
        notions = [n for n in (o_open[sym].get("notional"), g_open[sym].get("notional"))
                   if isinstance(n, (int, float)) and n > 0]
        consensus.append({
            "action": "open", "symbol": sym, "position_id": None,
            "notional": min(notions) if notions else None,
            "reason": f"đồng thuận OPUS+GROK: {(o_open[sym].get('reason') or '')[:35]} | "
                      f"{(g_open[sym].get('reason') or '')[:35]}",
        })

    # CLOSE — union by position_id (either agent can trigger an exit).
    closes: dict[int, dict] = {}
    for i in [*opus_intents, *grok_intents]:
        if i.get("action") == "close" and isinstance(i.get("position_id"), int):
            closes.setdefault(i["position_id"], i)
    for pid, i in closes.items():
        consensus.append({
            "action": "close", "symbol": i.get("symbol"), "position_id": pid,
            "notional": None, "reason": f"close (≥1 agent): {(i.get('reason') or '')[:55]}",
        })

    stats = {
        "opus_open": len(o_open), "grok_open": len(g_open),
        "agreed_open": len(set(o_open) & set(g_open)), "closes": len(closes),
    }
    return {"intents": consensus, "stats": stats}
