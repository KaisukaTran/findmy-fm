"""
OPUS orchestrator mode — an advanced, INDEPENDENT full-auto where Opus orchestrates
trades toward a 1% net/24h KPI, inside the same hard cage as the rule-based engine.

Spec: docs/opus-orchestrator-plan.md. This package is isolated from the rule-based
engine (app/scanner.py, app/scheduler.py) on purpose; toggling OPUS mode never alters
the existing full-auto. Phase O-0 is scaffolding only — no Opus API calls yet.
"""
