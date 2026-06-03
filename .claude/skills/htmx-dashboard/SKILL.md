---
name: htmx-dashboard
description: Patterns for building the FINDMY-FM dashboard with Jinja2 + HTMX + Alpine.js — partial endpoints, polling, WebSocket push, and tight-CSP asset handling. Load when working on app/templates, app/static, or partial routes.
---

# HTMX/Alpine Dashboard Patterns

Server-rendered, no SPA, no build step. Vendored assets (no runtime CDN) so CSP stays strict.

## Asset handling
- Put `htmx.min.js`, `alpine.min.js`, `style.css` in `app/static/`; reference via `/static/...`.
- CSP can stay `default-src 'self'` because nothing loads cross-origin. Avoid inline `<script>`; if a tiny inline script is unavoidable, keep it out of CSP-sensitive paths.

## Partial endpoints (return HTML fragments)
```python
@router.get("/partials/positions", response_class=HTMLResponse)
def positions_partial(request: Request, db: Session = Depends(get_db)):
    rows = list_positions(db)            # reuse domain fn
    return templates.TemplateResponse("partials/positions.html",
                                      {"request": request, "rows": rows})
```
Template polls it:
```html
<div hx-get="/partials/positions" hx-trigger="load, every 10s" hx-swap="innerHTML"></div>
```

## Mutations (approve/reject/start/stop)
Use `hx-post` on buttons; return the refreshed fragment to swap in place. Include the API key header via `hx-headers` (or rely on same-origin session for the local demo).
```html
<button hx-post="/api/pending/approve/{{o.id}}" hx-target="#pending-queue" hx-swap="outerHTML">Approve</button>
```

## WebSocket push (`/ws`)
Use HTMX ws extension or a small Alpine listener; on message, trigger `htmx.trigger(el, 'refresh')` on the affected section. Fall back to polling if the socket drops.

## Alpine for local state only
Modals, form show/hide, and the "Preview Pyramid" inputs/chart. Keep `x-data` objects tiny. Fetch preview via `hx-post /api/kss/preview` and render the returned table/fragment.

## Sections to render
Summary cards · Positions · Fills (trade history) · Pending queue (approve/reject) · KSS sessions (create/start/stop/delete/check-TP + Preview Pyramid).

Keep templates small and composable; one partial per dashboard section.
