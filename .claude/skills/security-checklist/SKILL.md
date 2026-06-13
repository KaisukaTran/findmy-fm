---
name: security-checklist
description: Security review checklist for FINDMY-FM changes — secrets, input validation, auth, headers/CORS, the approval gate, injection/SSRF, rate limiting. Load before doing a security pass on a phase.
---

# FINDMY-FM Security Checklist

Run through every item; report Severity | Location (`path:line`) | Issue | Fix; end with PASS / NEEDS-FIX.

## Secrets
- [ ] All secrets via `settings` (`SecretStr`); none hard-coded.
- [ ] No secret value logged, printed, or returned in any response/error.
- [ ] `.env.example` contains placeholders only; real `.env` is gitignored.

## Input validation
- [ ] Every request body/query has Pydantic types + constraints (`gt`, `ge`, `lt`, `le`, length).
- [ ] Domain bounds enforced: `distance_pct in (0,100)`, `max_waves>=1`, funds/prices/qty > 0.
- [ ] File/multipart uploads (if any) validate type + size; safe filenames.

## AuthN/Z
- [ ] Write endpoints (approve/reject, execute, kss create/start/stop/delete, patch) require the API-key header.
- [ ] Missing/invalid key → 401, with no detail leak.

## Headers & CORS
- [ ] Present: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, minimal CSP (`default-src 'self'`).
- [ ] CORS restricted to explicit localhost origins; not `*` with credentials.

## Approval gate (core safety invariant)
- [ ] No code path executes/fills an order without it passing pending → approve. Grep `execute`, `fill`, `Position(` callers.

## Injection / SSRF / unsafe ops
- [ ] SQLAlchemy ORM only — no string-formatted SQL.
- [ ] No `eval`/`exec`; `subprocess` only with fixed args, never user input.
- [ ] Outbound HTTP limited to Binance public API; no user-controlled URLs (SSRF).

## Resilience
- [ ] Rate limiting on write endpoints (slowapi).
- [ ] Errors return clean JSON; no stack traces / internal paths in prod responses.

## Quick greps
`SecretStr` · `eval(` · `exec(` · `subprocess` · `SELECT .*%`/f-string SQL · `logger.*(secret|token|key)` · `requests\.` / `open(` on request data · `allow_origins=\["\*"\]`.
