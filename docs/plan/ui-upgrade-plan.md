# UI Upgrade Plan (v1) — for builder agents

> Status: **PLAN ONLY — not implemented.** Author: planning session 2026-06-12.
> Scope: the dashboard UI — `app/templates/dashboard.html`, `app/templates/partials/*`,
> `app/static/app.js` (499 lines), `app/static/style.css`, plus the thin route layer that
> serves partials and `app/security.py` CSP only where the UI work touches it.
>
> **Hard constraint (user feedback, memory `ui-prefers-tables-not-cards`):** the user
> rejected a card-style redesign of the Trading tab as "quá xấu" — **keep tables for all
> dense data**. This plan contains NO visual redesign; it is correctness, efficiency,
> feedback UX, accessibility, and security polish. Any visual-direction change must be
> mocked and confirmed with the user first (AskUserQuestion with previews).
>
> **Karpathy discipline:** each phase lists exactly what to read. The whole UI is
> ~1,300 lines — a builder still reads only the files its phase names, returns summaries
> citing `path:line`, and keeps each phase to one small reviewable diff. Load the
> `htmx-dashboard` skill before building any phase; it documents the project's
> no-build-step, vendored-asset, tight-CSP rules.

## External references (patterns to borrow)

- htmx lazy-load + tabs examples — load content when shown, not all at once:
  https://htmx.org/examples/lazy-load/
- Pause polling on hidden browser tabs — `hx-trigger="every 5s [document.visibilityState === 'visible']"`:
  https://github.com/bigskysoftware/htmx/issues/824
- Lazy-loading hidden/modal content discussion (trigger on reveal, not on load):
  https://github.com/bigskysoftware/htmx/discussions/2736
- Client-side polling cancel patterns: https://github.com/bigskysoftware/htmx/issues/2489
- htmx release notes (vendored htmx version bump check, optional): https://github.com/bigskysoftware/htmx/releases

## Current architecture (verified 2026-06-12)

- One page, 6 plain-JS tabs (`showTab`, `app.js:305-317`); ~10 partials each poll
  `every 5–30s` **regardless of which tab is visible**; every mutation fires a global
  `refresh` event re-fetching all of them; WS `/ws` also triggers the same global refresh.
- All mutations go through one delegated click handler + `actions{}` map
  (`app.js:54-302`); feedback is `alert()`/`confirm()`/`prompt()`.
- CSP `default-src 'self'`, no inline scripts, vendored htmx/Alpine (CSP build —
  modals only). Server-rendered SVG charts (`app/charts.py`) — keep, CSP-perfect.

---

## Bug register

| # | Where | Bug | Severity |
|---|-------|-----|----------|
| U1 | `app/static/app.js:294-302` | The delegated dispatcher calls `fn(btn.dataset.id).catch(...)` — but sync actions (`closeLadder`, `auditFilter`, `auditFilterSymbol`, `auditClearSymbol`) return `undefined`, so `.catch` throws `TypeError` on **every click** of the audit filters / ladder close (after the action runs — symptom is console noise, not breakage). Wrap: `Promise.resolve(fn(...)).catch(...)`. | Med |
| U2 | `app/static/app.js:12` | `window.API_KEY` is read but **never set anywhere** (no template, no storage). With `REQUIRE_AUTH=true` every mutating button silently 401s — the dashboard only works with auth off. Decide: a one-time key prompt stored in `sessionStorage` + a header "🔑" indicator, or document loudly that the dashboard requires `require_auth=false`. (Recommend the former; tiny.) | High (when auth on) |
| U3 | `app/templates/dashboard.html:84-169` | All ~10 partials poll on their intervals **even when their tab panel is `display:none`** and when the browser tab is hidden. ~6×/min wasted requests + DB hits at idle, forever. Biggest UI efficiency win — see Phase P2. | High (efficiency) |
| U4 | `app.js:30-32` + every partial's `refresh from:body` | One mutation → global `refresh` → **all** partials + `loadParams()` refetch simultaneously (thundering herd on a SQLite-backed app; also races the scheduler's write lock). Scope refresh events per section (e.g. `refresh-trading`, `refresh-status`). | Med |
| U5 | `app/templates/partials/pending.html` (aa-max input) + backend | Known bug (memory `bug-autoapprove-max-resets`): the auto-approve max-notional UI value reverts to $50 after ~3 Set clicks — root cause is backend persistence (runtime_config), the input is the symptom. Fix backend persist + render the saved value; UI shows saved-confirmation. | Med |
| U6 | `app.js:398-413, 450-468` | `renderPreview` / `renderParamsRows` / `renderMlStatus` build `innerHTML` from **unescaped** API strings (`r.symbol`, `m.id`). Data is internal today, but it's the only place the UI bypasses Jinja autoescaping. Add a 3-line `esc()` helper and use it. | Low (defense-in-depth) |
| U7 | `app.js` toggles (e.g. `toggleAuto:94`, `toggleFullAuto:134`, `toggleGuardian:225`) | Every toggle is GET-state-then-POST-inverse — two round-trips and a read-modify-write race (two tabs, or scheduler changing state between GET and POST). The rendered badge already knows the current state: pass desired state via `data-id="on|off"` from the template and POST it directly. | Med |
| U8 | `dashboard.html:74-81`, `app.js:317` | Active tab resets to "Tổng quan" on every reload; no `location.hash` persistence. One-liner each way in `showTab`. | Low |
| U9 | `dashboard.html:28-71` | Modals: no ✕ close, no Esc handling, forms don't close or confirm on success (only a silent refresh, or an `alert`). `order-form` resets, `kss-form` doesn't. Add per-modal close + success behavior. No visual redesign — same dashed panel. | Low |
| U10 | all partial divs | No loading or error states: before first load a panel is blank; when a poll fails the stale table stays with **no indication**. Add a shared `htmx:responseError`/`htmx:sendError` listener → small status chip (e.g. "⚠ mất kết nối, đang thử lại") + `.htmx-indicator` on first load. Also surface WS drop (reconnect loop at `app.js:417-427` is silent). | Med |
| U11 | `dashboard.html:8-10` | `htmx.min.js` loads **synchronously in head** (render-blocking) while app.js/alpine are `defer`. htmx initializes on DOMContentLoaded; `defer` it too, keeping order before alpine. | Low |
| U12 | `dashboard.html:2` + templates | `<html lang="en">` but the UI is Vietnamese-first → wrong screen-reader voice, spellcheck, translation prompts. Set `lang="vi"`. Also: no favicon → permanent 404 noise in logs/devtools (add a 1-file SVG favicon, vendored). | Low |
| U13 | templates everywhere | Heavy inline `style="..."` (every panel/margin/badge color override). Works, but blocks ever tightening `style-src`, and duplicates values CSS already owns. Mechanical sweep into `style.css` classes **with zero visual change** (verify with before/after screenshots). | Low |
| U14 | a11y (cross-cutting) | Tabs are plain buttons (no `role=tablist/tab`, no arrow-key nav, no `aria-selected`); overlay modal has no focus trap/`aria-modal`; status badges are color-only for on/off (badge text does include ON/OFF — keep that). Minimal ARIA pass, no layout change. | Low |
| U15 | i18n consistency | Mixed EN/VI: headers Vietnamese ("Hàng chờ duyệt") but actions English ("Approve all", "Run scan", "Queue manual order"), confirms mostly English, alerts mixed. Standardize: VI-first labels/confirm texts, EN domain terms (TP/SL/DCA/notional) kept as-is. Pure template/string sweep. | Low |

**Explicit non-issues** (checked, leave alone): server-rendered SVG charts (CSP-perfect,
keep); the `#audit-wrap` filter-outside-the-swap pattern (`dashboard.html:153-167` —
deliberate and correct); Alpine-CSP-build for modals only; `kss-settings` panel loading
once (`hx-trigger="load"`) so polling can't clobber a half-edited form — that's a fix,
not a bug (`dashboard.html:138-140`); tables-not-cards layout (user preference).

---

## Phases

### Phase P1 — JS correctness sweep (U1, U6, U7, U11)
- **Agent**: `frontend-htmx`. **Read**: `app/static/app.js` (whole file), `dashboard.html:1-20`,
  one example toggle badge in `partials/status.html`. Skill: `htmx-dashboard`.
- **Work**: Promise-wrap the dispatcher (U1); `esc()` helper applied in the 3 JS renderers
  (U6); convert toggles to direct-POST with desired state from the template badge — touches
  `app.js` actions + the `data-action` buttons in `status.html`/`kss_settings.html`/
  `opus.html`/`scanner.html`/`pending.html` (U7 — keep the confirm() prompts for arming
  switches for now, P3 replaces the mechanism); `defer` htmx (U11).
- **Acceptance**: zero console errors clicking every audit filter / ladder close; toggling
  any switch is a single POST in the network tab; htmx still initializes (all panels load).
- **Verify**: `/verify` flow — run the app, click through every action button on all 6 tabs.

### Phase P2 — Polling efficiency: lazy tabs + scoped refresh (U3, U4, U8)
- **Agent**: `frontend-htmx`; this is the highest-value phase.
- **Read**: `dashboard.html` (whole), `app.js:30-44, 304-323, 415-427`; htmx refs above.
- **Work**:
  1. Visibility-gate every poll: `hx-trigger="load, every 10s [document.visibilityState === 'visible']"`
     (browser-tab level, htmx issue #824 pattern).
  2. App-tab level: panels of an inactive tab must not poll. Cleanest no-build approach:
     `showTab` toggles a `data-active` attribute and triggers `htmx.trigger(panel, 'tab-shown')`;
     partial divs use `hx-trigger="tab-shown from:closest [data-tab-panel], every Ns [tabActive(this)]"`
     — builder picks the simplest working combination of conditional-polling guard +
     custom event; must degrade to today's behavior if JS guard fails. The Overview tab
     loads eagerly (it's the landing tab).
  3. Split the global `refresh` into scoped events: `refresh-status` (status+summary),
     `refresh-trading` (kss/pending/positions/trades), `refresh-scanner`, `refresh-opus`,
     `refresh-losses`, `refresh-audit`, `refresh-params`. `refreshAll()` stays for WS
     pushes; each mutation action fires only its scope(s). Mapping table for builders:
     approve/reject/orders → trading+status; scan → scanner+status; kss* → trading+status;
     toggles → status (+ their own panel); closePosition → trading+losses+status.
  4. `location.hash` tab persistence (U8): `showTab` writes hash, init reads it.
- **Acceptance**: with the app idle on the Trading tab, the network tab shows **only**
  trading-tab + header polls (status/summary) — nothing from OPUS/losses/scanner; hiding
  the browser tab stops all polling within one interval; approving one order refetches
  only trading-scope partials; reload restores the active tab.
- **Out of scope**: changing any poll interval (tune later with real data).

### Phase P3 — Feedback layer: toasts, errors, modal UX (U9, U10, parts of U7's confirms)
- **Agent**: `frontend-htmx`.
- **Read**: `app.js` actions + forms sections; `style.css`; `partials/status.html` (for
  where the connection chip lives). No template redesign.
- **Work**:
  1. Tiny toast system (~40 lines JS + CSS, vendored, no lib): `toast(msg, kind)` with a
     fixed-position stack; replace every success/info `alert()` (12+ call sites).
     Keep `confirm()` for destructive/arming actions (delete session, FULL-AUTO, close
     position) — converting those to custom dialogs is P5-optional, **ask the user first**.
  2. Global `htmx:responseError` / `htmx:sendError` / WS-drop handler → one persistent
     connection chip in the header (`partials/status.html` area): "● live / ⚠ lỗi tải /
     ↻ đang kết nối lại". Auto-clears on next success.
  3. `.htmx-indicator` spinners on first load of each panel ("Đang tải…" muted row).
  4. Modal polish: ✕ button, Esc-to-close (one keydown listener), close+toast on submit
     success, `kss-form` resets like `order-form`.
- **Acceptance**: kill the server while the dashboard is open → chip flips to error state,
  no alert storm, recovers on restart; every former success-alert appears as a toast;
  Esc closes any open modal.

### Phase P4 — Auth + CSP hardening (U2, U13) — small, security-flavored
- **Agent**: `frontend-htmx` builds; `security-reviewer` audits after (load
  `security-checklist`; focus: key handling in storage, CSP delta, escaping from P1).
- **Work**:
  1. U2: on first 401 from `api()`, prompt once for the API key, keep in `sessionStorage`
     (NOT localStorage — session-scoped is the right trade-off for a local desk),
     retry the failed call; header shows 🔑 when a key is loaded; "đăng xuất khóa" action
     clears it. Partial GET routes stay keyless (read-only, by design — reviewer confirms).
  2. U13: sweep inline `style=` attributes into classes (mechanical, zero visual change;
     screenshot diff per tab as evidence). THEN tighten CSP in `app/security.py:30`:
     `style-src 'self'` (drop `'unsafe-inline'`) — only after the sweep proves clean.
     If any third-party (htmx) injects inline styles for indicators, scope the exception
     narrowly (`style-src-attr`) and document why.
- **Acceptance**: with `REQUIRE_AUTH=true` the dashboard is fully operable after one key
  prompt; CSP report-only run shows zero style violations before flipping enforce;
  security-reviewer sign-off note in the PR.

### Phase P5 — Language, a11y, chrome (U5, U12, U14, U15)
- **Agent**: `frontend-htmx`; backend half of U5 → `backend-builder` (persist
  `autoapprove_max_notional` in runtime_config, read it back on render — see memory
  `bug-autoapprove-max-resets`).
- **Work**: `lang="vi"` + SVG favicon (U12); ARIA roles/keyboard for tabs + focus trap in
  the overlay modal (U14); VI-first string sweep with EN domain terms kept (U15) — one
  pass over templates + `app.js` confirm/alert/toast strings; U5 fix + the input renders
  the persisted value.
- **Acceptance**: tab key navigates tabs with arrow keys; NVDA/VoiceOver announces VI;
  the aa-max value survives 5 Set clicks + a restart; no favicon 404 in the log.

### Phase P6 (optional, needs user sign-off) — New surfaces
Deliberately thin — propose, don't build, until the user picks:
- Scanner panel footer (scan duration / evaluated / skipped / cache hit-rate) — this is
  **S6 of `docs/plan/scanner-upgrade-plan.md`**; build it there, render it here.
- A "skipped: why" expander per scanner row (the reason string already exists in
  `Candidate.reason` — surfacing is one `<details>` cell; tables preserved).
- Mobile audit: the 8-badge statusbar wraps badly under 600px — propose a collapsed
  "Automation ▾" popover, **mock first** (memory: confirm visual direction before building).

## Sequencing & ownership

| Phase | Depends on | Lead agent | Size |
|-------|-----------|------------|------|
| P1 JS correctness | — | frontend-htmx | app.js + 4 partials, small |
| P2 lazy tabs + scoped refresh | P1 (dispatcher fix) | frontend-htmx | dashboard.html + app.js |
| P3 toasts/errors/modals | P1 | frontend-htmx | app.js + style.css + status.html |
| P4 auth + CSP | P1 (esc), U13 sweep | frontend-htmx + security-reviewer | templates sweep + security.py 1 line |
| P5 a11y/i18n/aa-max | — (anytime) | frontend-htmx + backend-builder (U5) | strings + small backend |
| P6 new surfaces | user sign-off | frontend-htmx | TBD |

P2 and P3 are independent after P1. Every phase: run the app and click through all six
tabs before committing (`/verify`); commit per phase at green (`fm-conventions`).
No screenshots of "redesigns" — this plan changes no layout; if a phase accidentally
shifts visuals, that's a regression, not a feature.

## Open questions for the user (ask at build time)
1. P3: replace `confirm()` on destructive/arming actions with styled non-blocking dialogs,
   or keep native confirms? (Native is uglier but unambiguous; recommend keep for v1.)
2. P4: is `REQUIRE_AUTH=true` actually used on this desk today? If never, U2 can be a
   documented limitation instead of code.
3. P6: which (if any) of the three proposed surfaces to build, and mock approval for the
   mobile statusbar popover.
