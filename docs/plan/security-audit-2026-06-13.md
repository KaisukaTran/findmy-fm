# Security Audit — Go-Live Phase 2.1 (2026-06-13)

Scope: pre-go-live hardening pass over the lean `app/` tree using
`.claude/skills/security-checklist`. Read-only audit, no behaviour change.

## Result: **PASS** — 0 high-severity open.

| Severity | Location | Finding | Disposition |
|----------|----------|---------|-------------|
| PASS | `app/security.py:43-44` | API key compared with `hmac.compare_digest` (constant-time); key stored as `SecretStr`. | OK |
| PASS | `app/security.py:30-36` | CSP `default-src 'self'; img-src 'self' data:; style-src 'self'` + `nosniff`/`DENY`/`no-referrer` headers on every response. | OK |
| PASS | `app/config.py:37-39` | CORS restricted to explicit localhost origins — no `*` with credentials. | OK |
| PASS | `app/security.py:28,70` | Global rate limit (200/min) via slowapi `SlowAPIMiddleware`; 429 returns clean JSON. | OK |
| PASS | `app/routes.py`, `app/kss/routes.py` | Every mutation endpoint (orders, approve/reject[-all], auto, autoapprove, scan, kss create/start/stop/patch/delete/check-tp/dca-next) carries `dependencies=[Depends(require_api_key)]`. `/kss/preview` is read-only (computes a ladder, no DB write) so ungated is fine. | OK |
| PASS | `app/guardian.py:21`, `app/notify.py:38,55` | Outbound HTTP targets are fixed module constants (`api.anthropic.com`, `api.telegram.org/bot{token}`). No user-controlled URL → no SSRF. | OK |
| PASS | `app/config.py` | All secrets are `SecretStr`; `.env.example` holds placeholders only (`change_this…`, empty values). No secret is logged or returned. | OK |
| PASS | repo-wide | No `eval`/`exec`/`subprocess` on request data; SQLAlchemy ORM only (no f-string SQL). | OK |
| Info (go-live carry-forward) | `app/config.py:33-35` | `require_auth` defaults **False** ("off by default for local demo"), so write endpoints are no-op-gated until enabled. Acceptable for a localhost paper demo. | **Phase 6.3**: when `LIVE_TRADING` is enabled, force `require_auth=True` (or refuse to boot the live path) so real-money endpoints can't be hit unauthenticated. |

## Approval-gate invariant
No execute/fill path bypasses pending → approve. Frozen-breaker auto-paths return
empty (`test_killswitch.py`); the only intentional exception is **SELL exits**, which
must not be blocked (`test_drawdown_fixes.py`, by design — exits reduce exposure).

## Conclusion
Ship-ready for the paper/default-OFF go-live infra. The single carry-forward
(`require_auth` ↔ `LIVE_TRADING`) is folded into Phase 6.3, not a current vulnerability.
