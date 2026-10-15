# Vendored third-party assets

Served same-origin so the dashboard CSP (`script-src` falls back to `'self'`, app/security.py) never
has to allow a CDN. Every file here is pinned and hash-checked by `tests/app/test_vendor_assets.py`.
To upgrade: fetch the new tarball from the npm registry, verify its `dist.integrity`, re-scan the
bundle (no eval / new Function / fetch / WebSocket / style injection beyond the attribution logo),
replace the file under a new versioned name, and update the hashes below and in the test.

## lightweight-charts 5.2.1 (TradingView, Apache-2.0)

| | |
|---|---|
| File | `lightweight-charts-5.2.1.standalone.production.js` (197,922 bytes) |
| Source | `https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz` → `package/dist/lightweight-charts.standalone.production.js` |
| Tarball integrity (npm) | `sha512-IVwoK1RLFiLPubaKIjNbtjWLnpPMqiABSrTay6whmNa8L1+19292VtHJ+BWyPUuLCwF0tcQlhEWd1CLB2a1nsQ==` (verified locally 2026-10-13) |
| File sha256 | `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf` |
| License | `lightweight-charts.LICENSE` (Apache-2.0). Attribution is shown as a text credit in the chart footer; the library's own logo is disabled (`layout.attributionLogo: false`) because it injects a `<style>` element the CSP refuses. |
| Bundle scan 2026-10-13 | 0 × eval / new Function / fetch / XMLHttpRequest / WebSocket / sendBeacon / cookie / localStorage. The single `createElement("style")` + `innerHTML` build the attribution logo (static SVG). |
