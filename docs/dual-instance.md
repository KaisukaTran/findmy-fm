# Running paper + live in parallel (isolated instances)

Two independent app instances run side by side on this machine:

- **paper** — the staging / experiment environment. ALL development and testing happens here.
- **live** — production, **frozen** code that only ever receives reviewed merges. **Never edit or
  test directly on live.** Starts on Binance **testnet** first.

They share the same git repo and `.venv` but are otherwise fully isolated:

| | paper | live |
|---|---|---|
| git worktree / branch | `d:\FINDMY` @ `dev` (or a feature branch) | `d:\FINDMY-live` @ `live` |
| port (dashboard) | `8000` | `8001` |
| DB (`DATABASE_URL`) | `data/findmy.db` (existing book) | `data/live.db` (fresh) |
| scheduler lock (`SCHEDULER_LOCK_PORT`) | `8801` | `8802` |
| `LIVE_TRADING` | `false` | `true` |
| `LIVE_USE_TESTNET` | — | `true` |
| exchange keys | none | testnet (in the live worktree `.env`) |
| launcher | `scripts/run_paper.ps1` | `scripts/run_live.ps1` |

The two **distinct scheduler lock ports** are what let both schedulers run at once — same port across
two processes = only one scheduler (the cross-process mutex in `app/scheduler.py`). `LIVE_TRADING=false`
+ no keys means the paper instance physically cannot place a real order
(`app/execution.py:live_enabled`), and separate DBs mean a paper experiment can never touch the live
book.

## One-time setup

```powershell
# 1) create the stable live branch from the current reviewed tip, and a worktree for it
git branch live                       # or: git branch live <reviewed-commit>
git worktree add ../FINDMY-live live  # creates d:\FINDMY-live on branch `live`

# 2) give the live worktree its own env with TESTNET keys
cd d:\FINDMY-live
copy ..\FINDMY\.env.live.example .env
#   then edit .env → paste your https://testnet.binance.vision HMAC keys

# 3) (optional) a `dev` branch for paper experiments
cd d:\FINDMY
git branch dev ; git checkout dev
```

## Daily run

```powershell
# paper (terminal 1, from d:\FINDMY)
pwsh -File scripts/run_paper.ps1     # http://127.0.0.1:8000

# live  (terminal 2, from d:\FINDMY-live)
cd d:\FINDMY-live ; pwsh -File scripts/run_live.ps1   # http://127.0.0.1:8001
```

To restart cleanly, stop the old process for that port first (see `app-run-restart-ops`): kill the
PID listening on the port, then re-run the script. The header shows a **PAPER** / **LIVE · TESTNET**
badge so the two dashboards are never confused.

## Promotion workflow (paper → live)

1. Develop + test only on `dev` / feature branches in the **paper** worktree.
2. Confirm in paper: `pytest tests/app/ -o addopts=""` green + behaviour verified on `:8000`.
3. Promote (reviewed, never edited on live):
   ```powershell
   git checkout live
   git merge --no-ff dev        # or open a PR dev → live and /code-review it
   ```
4. In the live worktree, pull + restart:
   ```powershell
   cd d:\FINDMY-live ; git pull ; pwsh -File scripts/run_live.ps1
   ```
5. The `live` branch is only ever fast-forwarded with reviewed commits — no direct edits.

## Caveats
- **Testnet keys required** for the live instance to actually place (testnet) orders; without them it
  just runs as a second paper app.
- **Live maker trading is not functional yet** — async order tracking + the resting-maker model
  (live-readiness tasks 1.4/1.5, see `docs/plan/live-readiness-plan.md`) are still pending, so a
  non-immediately-filled live limit currently raises (the safe "no fill price" path). The live testnet
  instance today validates the *plumbing*; finish 1.4/1.5 against it before switching to real funds.
