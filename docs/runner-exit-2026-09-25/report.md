# Runner-exit study — does letting the KSS take-profit float beat selling at the limit?

Generated 2026-09-25T04:46:54.231505+00:00Z. Config: distance 7.0%, 10 rungs max, TP 5.0%+0.5%/rung, no stop-loss, 60-day deadline, wave0 $28, maker 0.1% / taker 0.1%, stop slippage [0.1, 0.3]%.

## What this means

V0 is the production behaviour: sell the whole position the instant the resting take-profit limit is touched. Every other variant answers 'what if we didn't sell there' under a specific rule, each checked under BOTH intra-bar orderings (optimistic and pessimistic — a single bar's true high/low order is unknowable) and, for every stop-based exit, both a 0.1% and 0.3% slippage assumption. The headline tables below pick each variant's best-performing SETTING by its PESSIMISTIC-bound result, not its best case, per the brief. A variant only 'guarantees no loss' if its no-loss-violation count is 0 across every setting and bound checked — see the audit table.

### Backtest population (primary) — best setting per variant (chosen by the PESSIMISTIC bound)

| variant | param | slip | N | mean $/event | mean diff vs V0 | 95% CI (symbol-boot) | median diff | % worse than V0 | worst event | top-2 sym share | $/capital-day | no-loss violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| V0 | None | None | 53299 | +7.418 | +0.000 | [+0.000, +0.000] | +0.000 | 0.0% | +1.343 (ALGOUSDT) | 4.4% | +1.614574 | 0 |
| V1 | 5.0 | 0.1 | 53257 | +7.257 | -0.166 | [-0.303, -0.021] | -0.328 | 62.9% | -98.594 (WIFUSDT) | 4.5% | +0.118153 | 134 |
| V2 | 3.0 | 0.1 | 53186 | +7.136 | -0.292 | [-0.462, -0.130] | +0.816 | 28.7% | -146.814 (WIFUSDT) | 4.6% | +0.060657 | 178 |
| V3 | k=1.0 | n/a | 52588 | +18.254 | n/a | n/a | +4.244 | n/a | -14680.078 (RUNEUSDT) | n/a | dl-loss 1019 | NOT no-loss-guaranteed
| V4a | 3.0 | 0.1 | 53299 | +7.248 | -0.170 | [-0.369, +0.025] | -0.474 | 64.9% | -243.256 (WIFUSDT) | 4.6% | +0.167321 | 341 |
| V4b | None | 0.1 | 53299 | +6.948 | -0.470 | [-0.718, -0.221] | -0.300 | 66.8% | -243.256 (WIFUSDT) | 5.0% | +0.102762 | 341 |

### Real paper events (secondary, sanity) — best setting per variant (chosen by the PESSIMISTIC bound)

| variant | param | slip | N | mean $/event | mean diff vs V0 | 95% CI (symbol-boot) | median diff | % worse than V0 | worst event | top-2 sym share | $/capital-day | no-loss violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| V0 | None | None | 49 | +2.591 | +0.000 | [+0.000, +0.000] | +0.000 | 0.0% | +0.704 (MORPHO) | 32.2% | +16.787362 | 0 |
| V1 | 8.0 | 0.1 | 45 | +3.524 | +0.992 | [+0.452, +1.677] | +0.200 | 44.4% | +0.151 (MORPHO) | 31.1% | +0.099765 | 0 |
| V2 | 8.0 | 0.1 | 43 | +4.256 | +1.671 | [+0.717, +2.423] | +2.343 | 32.6% | +0.000 (FIL) | 40.6% | +0.108349 | 0 |
| V3 | - | - | 0 | - | - | - | - | - | - | - | - | - |
| V4a | 5.0 | 0.1 | 49 | +4.136 | +1.546 | [+0.368, +2.532] | -0.150 | 55.1% | +0.738 (XPL) | 38.5% | +0.157012 | 0 |
| V4b | None | 0.1 | 48 | +2.630 | +0.112 | [-0.478, +0.817] | -0.273 | 66.7% | +0.755 (CRV) | 35.4% | +0.265309 | 0 |

### Backtest — no-loss audit (20903 total violations across every setting/bound/slip; worst 20 listed)

| symbol | variant | param | bound | slip | kind | net $ | net % | breakeven | fill |
|---|---|---|---|---|---|---|---|---|---|
| RUNEUSDT | V3 | 1.0 | OPT | None | deadline | -14680.078 | -55.792% | - | - |
| RUNEUSDT | V3 | 1.0 | PESS | None | deadline | -14680.078 | -55.792% | - | - |
| RUNEUSDT | V3 | 1.0 | OPT | None | deadline | -11031.394 | -30.055% | - | - |
| RUNEUSDT | V3 | 1.0 | PESS | None | deadline | -11031.394 | -30.055% | - | - |
| WLDUSDT | V3 | 1.0 | OPT | None | deadline | -10122.294 | -27.578% | - | - |
| WLDUSDT | V3 | 1.0 | PESS | None | deadline | -10122.294 | -27.578% | - | - |
| RUNEUSDT | V3 | 1.0 | PESS | None | deadline | -9444.179 | -30.055% | - | - |
| RAYUSDT | V3 | 1.0 | PESS | None | deadline | -8428.685 | -50.077% | - | - |
| STRKUSDT | V3 | 1.0 | OPT | None | deadline | -7993.238 | -47.490% | - | - |
| STRKUSDT | V3 | 1.0 | PESS | None | deadline | -7993.238 | -47.490% | - | - |
| CRVUSDT | V3 | 1.0 | OPT | None | deadline | -7835.345 | -21.348% | - | - |
| CRVUSDT | V3 | 1.0 | PESS | None | deadline | -7835.345 | -21.348% | - | - |
| RUNEUSDT | V3 | 0.5 | OPT | None | deadline | -7340.039 | -55.792% | - | - |
| RUNEUSDT | V3 | 0.5 | PESS | None | deadline | -7340.039 | -55.792% | - | - |
| CRVUSDT | V3 | 1.0 | OPT | None | deadline | -6707.983 | -21.348% | - | - |
| CRVUSDT | V3 | 1.0 | PESS | None | deadline | -6707.983 | -21.348% | - | - |
| ORDIUSDT | V3 | 1.0 | OPT | None | deadline | -6648.157 | -39.498% | - | - |
| ORDIUSDT | V3 | 1.0 | PESS | None | deadline | -6648.157 | -39.498% | - | - |
| TIAUSDT | V3 | 1.0 | OPT | None | deadline | -6167.361 | -36.642% | - | - |
| TIAUSDT | V3 | 1.0 | PESS | None | deadline | -6167.361 | -36.642% | - | - |

## Real-event dataset detail

- raw TP fills found: 338
- backup DB: used, 54,157,312 bytes
- sampled for the network pass: 54
- kept after the phantom filter: 49 / dropped: 5 (of which 0 were fetch failures — unknown, not confirmed phantom)
- events whose 60-day window reaches 'now' (still open, MTM only): 49

### XPL session 36 (the owner's example)
avg 0.0928, qty 1645.9, 3 rungs, TP fill 0.09859259808391956 — V0 net $+9.216 (+6.028%).