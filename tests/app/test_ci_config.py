"""Supply-chain hygiene of the CI config itself: third-party actions pinned to a commit SHA (a
moved tag or branch runs someone else's code with our token), the dependency audit is a real
gate, CI runs on the branch that actually ships (`live`), and Dependabot watches both pins."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = sorted((_ROOT / ".github" / "workflows").glob("*.yml"))


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_every_action_is_pinned_to_a_full_commit_sha():
    assert _WORKFLOWS
    for wf in _WORKFLOWS:
        for ref in re.findall(r"uses:\s*(\S+)", _text(wf)):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"{wf.name}: {ref}"


def _job(wf: str, name: str) -> str:
    """The text of one top-level job (2-space-indented key) of a workflow."""
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", wf, re.S | re.M)
    assert m, f"job {name!r} not found"
    return m.group(1)


def test_pip_audit_is_blocking_and_audits_the_runtime_list():
    job = _job(_text(_ROOT / ".github" / "workflows" / "tests.yml"), "audit")
    assert "pip-audit -r requirements-app.txt" in job
    assert "continue-on-error" not in job


def test_ci_runs_on_live():
    for wf in _WORKFLOWS:
        on = _text(wf).split("jobs:", 1)[0]
        assert "live" in on, f"{wf.name} does not trigger on the live branch"


def test_dependabot_watches_pip_and_actions():
    cfg = _text(_ROOT / ".github" / "dependabot.yml")
    assert 'package-ecosystem: "pip"' in cfg
    assert 'package-ecosystem: "github-actions"' in cfg
