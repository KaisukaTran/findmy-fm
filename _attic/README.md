# `_attic/` — staged for deletion, not yet deleted

Everything in here was moved by `git mv` on 2026-09-06 so it could be looked at once more before
it goes. Nothing in `app/` imports any of it; the test suite and both CI workflows never touch it.
It is kept as one directory instead of scattered through the tree so the decision is a single one.

**To delete it all:** `git rm -r _attic` — the history stays in git either way, so a later
`git log --follow` or `git show <sha>:src/findmy/...` still recovers any file.

## What is in here and why it stopped being used

### The v1 codebase (~21,000 LOC)
`docs/REBUILD.md` records the v2 rebuild: `app/` replaced `src/findmy/` + `services/`. The old
tree was simply never removed.

| path | what it was |
|---|---|
| `src/findmy/` | the v1 package (7,149 LOC), including **a second copy of the KSS pyramid math** — `src/findmy/kss/pyramid.py`. `app/kss/pyramid.py` is the frozen one CLAUDE.md protects; this copy has been unmaintained since June and is the reason this move happened at all. Two files with the same name and the same formulas, one of which nobody watches, is a trap for the next person who greps. |
| `services/` | the SOT/TS split-service architecture (2,900 LOC), explicitly collapsed into one `app/` by the rebuild |
| `tests/kss/`, `tests/test_*.py` | 10,708 LOC of v1 tests. Both CI workflows run `pytest tests/app` only, so these have not executed in months |
| `conftest.py`, `pytest.ini` (root) | existed only to bootstrap the v1 test DBs. The live config is `tests/app/pytest.ini` |
| `templates/`, `static/` (root) | mounted only by `src/findmy/api/main.py`. `app/` serves `app/templates/` + `app/static/` |
| `examples/*.xlsx` | sample purchase-order spreadsheets for a v1 upload feature that does not exist in `app/` |
| `db/migrations/` | the SOT SQL schema. `app/db.py` builds its own schema with `create_all()` + an additive column list and never reads these |
| `audits/` | a standalone audit-report renderer, zero references anywhere; unrelated to `app/audit.py` |
| `requirements.txt` | a 132-line `pip freeze` of the v1 environment. CI installs `requirements-dev.txt` + `requirements-prod.txt` |

Three files outside this directory pointed at `src/` and were repointed in the same commit:
`Dockerfile` (booted `src.findmy.api.main:app` — and CI's Docker step is `continue-on-error`, so
it would have failed silently), `pyproject.toml` (`packages.find where = ["src"]`), and
`pyrightconfig.json`.

### Spent one-shot scripts
Each did its job and its conclusion is recorded in a commit message or in the project memory.

| script | what it answered |
|---|---|
| `scripts/measure_entry_gates.py` | the six coin-entry gates measure worthless |
| `scripts/pyramid_up_backtest.py`, `scripts/pyramid_up_noflip_backtest.py` | pyramid-up baseline and the flip follow-up |
| `scripts/regime_filter_study.py` | a regime filter does not rescue the pyramid |
| `scripts/liquidity_tier_study.py` → **NOT here**, it is a library three live scripts import |
| `scripts/live_mechanism.py`, `scripts/sl_slippage.py`, `scripts/matrix_3y.py` | backtests against the real live mechanism; SL fills are perfect in the simulator (biased toward wide stops); the 3-year rung × step × SL matrix |
| `scripts/replay_scans.py` | Phase S4 validation, superseded by the weights now in `DEFAULT_WEIGHTS` |
| `scripts/observe_full_auto.py` | an offline harness from when this machine could not reach Binance |
| `scripts/consolidate_fet.py` | merged FET sessions #145 → #144, once |
| `scripts/test_tls_frag.py` | the TLS-fragmentation probe for the Telegram SNI block, superseded by `docs/telegram-cloudflare-proxy.md` |
| `scripts/merge_worktrees.ps1` | already executed. It deletes `D:\FINDMY-live`; leaving it in `scripts/` was a loaded gun |
| `scripts/start_api.sh`, `scripts/test_sot_dal.py` | v1 launcher and v1 DAL smoke test |

## What was deliberately NOT moved

- `scripts/liquidity_tier_study.py` — filed as a "study" but imported by `cross_section_ic.py`,
  `ladder_panel_study.py` and `dynamic_exit_panel.py`. Archiving it breaks the live research panel.
- `scripts/rearm_dead_ladders.py`, `scripts/testnet_lib.py` — both loaded by tests through
  `importlib.util.spec_from_file_location`; moving either reds the suite.
- Every database under `data/` — including `data/backups/` (278 MB) and
  `data/research/market.db` (301 MB), which together are the biggest reclaim available and are
  the operator's call, not this cleanup's.
- `.env.bak-*` / `.env.paper-backup*` — they contain live API secrets. They should be shredded
  rather than deleted, and that is a decision to make deliberately.
