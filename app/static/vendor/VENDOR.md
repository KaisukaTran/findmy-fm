# Vendored third-party assets

Served same-origin so the dashboard CSP (`script-src 'self'`, app/security.py) never has to allow a
CDN. Every file here is pinned and hash-checked by `tests/app/test_vendor_assets.py`.
To upgrade: fetch the new tarball from the npm registry, verify its `dist.integrity`, re-scan the
bundle (no eval / new Function / fetch / WebSocket / style injection beyond the attribution logo),
replace the file under a new versioned name, and update the hashes below and in the test.

## htmx 2.0.3 (Big Sky Software, 0BSD)

| | |
|---|---|
| File | `htmx-2.0.3.min.js` (50,387 bytes) — was `app/static/htmx.min.js` until 2026-10-15 (same bytes) |
| Source | `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.3.tgz` → `package/dist/htmx.min.js` |
| Tarball integrity (npm) | `sha512-AeoJUAjkCVVajbfKX+3sVQBTCt8Ct4lif1T+z/tptTXo8+8yyq3QIMQQe/IT+R8ssfrO1I0DeX4CAronzCL6oA==` (verified locally 2026-10-13) |
| File sha256 | `491955cd1810747d7d7b9ccb936400afb760e06d25d53e4572b64b6563b2784e` |
| License | `htmx.LICENSE` (Zero-Clause BSD, from the tarball) |
| Bundle scan 2026-10-15 | 1 × `eval(` + `new Function` — reached only through `allowEval` (hx-on, `js:` values, trigger filters), which `dashboard.html` turns off via `htmx-config` and the CSP refuses anyway. 2 × XMLHttpRequest = htmx's own requests, same-origin under `connect-src 'self'`. `localStorage` = htmx's history cache. 0 × fetch / WebSocket / EventSource / cookie. Its injected indicator `<style>` is disabled (`includeIndicatorStyles: false`). |

## Alpine.js 3.14.1, CSP build (Caleb Porzio and contributors, MIT)

| | |
|---|---|
| File | `alpine-csp-3.14.1.min.js` (45,228 bytes) — was `app/static/alpine.min.js` until 2026-10-15. The old file's *checkout* hashed differently only because `core.autocrlf` rewrote its LF line endings; the git blob was already these bytes. |
| Source | `https://registry.npmjs.org/@alpinejs/csp/-/csp-3.14.1.tgz` → `package/dist/cdn.min.js` |
| Tarball integrity (npm) | `sha512-tNbXcAWDvRmzM/Cxx2HBpHg9kOGmRTYSw/RziA4kUFBu9rJfKk4vflrLr3gLGu1ocji4CQq1Zn44iv1TsBA8lQ==` (verified locally 2026-10-13) |
| File sha256 | `40f00d7aaa543eabd17c3a0f79ce2ac62e6be17caa9564527c20ec39152bb157` |
| License | `alpine.LICENSE` (MIT). The npm package ships none; this is `LICENSE.md` from github.com/alpinejs/alpine at tag `v3.14.1`. |
| Bundle scan 2026-10-15 | 0 × eval / new Function / fetch / XMLHttpRequest / WebSocket / EventSource / cookie / localStorage / style injection — the CSP build evaluates expressions without `Function`. |

## lightweight-charts 5.2.1 (TradingView, Apache-2.0)

| | |
|---|---|
| File | `lightweight-charts-5.2.1.standalone.production.js` (197,922 bytes) |
| Source | `https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz` → `package/dist/lightweight-charts.standalone.production.js` |
| Tarball integrity (npm) | `sha512-IVwoK1RLFiLPubaKIjNbtjWLnpPMqiABSrTay6whmNa8L1+19292VtHJ+BWyPUuLCwF0tcQlhEWd1CLB2a1nsQ==` (verified locally 2026-10-13) |
| File sha256 | `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf` |
| License | `lightweight-charts.LICENSE` (Apache-2.0). Attribution is shown as a text credit in the chart footer; the library's own logo is disabled (`layout.attributionLogo: false`) because it injects a `<style>` element the CSP refuses. |
| Bundle scan 2026-10-13 | 0 × eval / new Function / fetch / XMLHttpRequest / WebSocket / sendBeacon / cookie / localStorage. The single `createElement("style")` + `innerHTML` build the attribution logo (static SVG). |
