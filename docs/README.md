# FINDMY-FM — Documentation Index

Documentation for the v2 lean rebuild (`app/`). Start with the
[root README](../README.md) for an overview and quick start, then dive in here.

## Core docs

| Document | What it covers |
|----------|----------------|
| [REBUILD.md](REBUILD.md) | v2 architecture, what changed from the legacy v1 tree, and the Claude Code agent/skill workflow. |
| [kss.md](kss.md) | **KSS Pyramid DCA** strategy — wave formulas, session lifecycle, REST endpoints. |
| [AGENTS.md](AGENTS.md) | **Multi-agent scanner** + backtested auto-trading: universe → backtest → vote → gate → act. |
| [go-live.md](go-live.md) | Operator runbook to arm the **real-money** execution path (shipped OFF). |
| [telegram-setup.md](telegram-setup.md) | Telegram bot setup, command reference, and security boundary. |

## Plans & proposals

Working documents for in-progress or proposed features. They describe intent, not
necessarily shipped behavior.

- [plan/go-live-plan.md](plan/go-live-plan.md) — go-live hardening plan.
- [plan/telegram-notify-plan.md](plan/telegram-notify-plan.md) — Telegram notification/control plan.
- [plan/ux-tweaks-plan.md](plan/ux-tweaks-plan.md) — dashboard UX tweaks.
- [plan/security-audit-2026-06-13.md](plan/security-audit-2026-06-13.md) — security audit notes.
- [opus-orchestrator-plan.md](opus-orchestrator-plan.md) — OPUS orchestrator mode (advanced, paper-only).
- [kss-accounting-upgrade-plan.md](kss-accounting-upgrade-plan.md) — KSS accounting-consistency proposal.

## History

- [devlog/](devlog/) — early development journal (day-by-day).
- [archive/](archive/) — superseded v0.1 implementation summaries, kept for reference.

## Configuration

The canonical configuration reference is [`.env.example`](../.env.example) at the
repository root — every setting is listed and commented there. Settings are
defined in `app/config.py`.
