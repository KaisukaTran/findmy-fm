---
name: context-engineering
description: PRIMARY working discipline for this project (Karpathy-style). Apply by default on every task — fill the context window with just the right information, nothing more. Load when planning, delegating to subagents, reviewing diffs, or whenever context is growing. Optimizes tokens and decision quality.
---

# Context Engineering (Karpathy) — the default discipline

> "Context engineering is the delicate art and science of filling the context
> window with just the right information for the next step." — Andrej Karpathy.
> The LLM is the CPU; the context window is the RAM. Treat it like memory you
> must budget. Beware **context rot**: as the window grows, recall & reasoning
> degrade — more tokens ≠ better.

This is the **main skill** for FINDMY-FM work. Use it on every task.

## The loop: Write → Select → Compress → Isolate

- **Write** — put durable facts where they belong, not in the live window:
  memory files for standing facts, the plan file for the plan, code comments for
  rationale. Don't re-explain what's already written; link to it.
- **Select** — pull in only what the next step needs. Read the specific lines/
  functions, not whole files. Load a skill on demand instead of carrying it.
- **Compress** — summarize tool output, logs, and search results to the
  conclusion + `path:line`. Never paste large blobs you won't act on.
- **Isolate** — push big, noisy, or parallel work into subagents that return a
  short summary, not raw dumps. Keep nesting shallow (≤2–3 levels).

## Concrete rules for this repo

1. Reading: target the region you need (`Read` with offset/limit, `Grep`), not
   the whole file. Don't re-read a file you just edited.
2. Tool output: when a command is noisy (pytest, ruff, ccxt), filter to the
   verdict + failing lines. Prefer `test-runner`/Explore agents that report tersely.
3. Subagents return **compressed summaries that cite `path:line`** — never file
   dumps. Mechanical work → sonnet/haiku; architecture/security → opus.
4. Prefer text/SVG/structured summaries over dumping raw data into context.
5. Small, verifiable diffs (1–3 files); commit at green checkpoints.
6. Durable knowledge goes to memory or docs (Write), not repeated in chat.
7. Skills are progressive disclosure: keep them short; load only when relevant.
8. When the window is getting long, compress/handoff rather than carrying
   everything forward.

## Why it matters here
Token-optimal context = lower cost, less context rot, sharper decisions — which
is exactly what a precision-first, loss-minimizing trading tool needs from its
builder. See also [[karpathy-context-engineering-default]].

Reference: Karpathy on context engineering; davidkimai/Context-Engineering handbook.
