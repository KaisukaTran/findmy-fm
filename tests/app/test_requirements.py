"""requirements-app.txt is the ONE runtime dependency list (Dockerfile, CI, dev all install it).
Every third-party module app/ imports must be declared there — a clean install from it once
lacked httpx (imported at the top of app/notify.py) and websockets (app/data/ws_feed.py)."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# Imported, but installed by another declared requirement that pins it.
_PROVIDED_BY = {"starlette": "fastapi"}
# Lazy, feature-flagged imports that are allowed to be absent (app/ta/lib.py, ta_lib_enabled).
_OPTIONAL = {"pandas", "pandas_ta"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _declared(path: Path) -> dict[str, str]:
    """{normalised name: pinned version} for every `name[extras]==version` line."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s;]+)", line)
        if m:
            out[_norm(m.group(1))] = m.group(2)
    return out


def _third_party_imports() -> set[str]:
    mods: set[str] = set()
    for f in (_ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in sys.stdlib_module_names and m != "app"}


def test_every_app_import_is_declared_in_requirements_app():
    declared = _declared(_ROOT / "requirements-app.txt")
    missing = sorted(
        m for m in _third_party_imports() - _OPTIONAL
        if _norm(_PROVIDED_BY.get(m, m)) not in declared
    )
    assert missing == [], f"imported by app/ but not pinned in requirements-app.txt: {missing}"


def test_every_runtime_line_is_pinned_exactly():
    for line in (_ROOT / "requirements-app.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            assert "==" in line, f"unpinned runtime requirement: {line!r}"


def test_jinja2_is_past_the_sandbox_cves():
    major, minor, patch = (int(x) for x in _declared(_ROOT / "requirements-app.txt")["jinja2"].split("."))
    assert (major, minor, patch) >= (3, 1, 6)


def test_there_is_one_runtime_list_and_everything_installs_it():
    assert not (_ROOT / "requirements-prod.txt").exists(), "retired: use requirements-app.txt"
    assert "requirements-app.txt" in (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "-r requirements-app.txt" in (_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for wf in (_ROOT / ".github" / "workflows").glob("*.yml"):
        assert "requirements-prod" not in wf.read_text(encoding="utf-8"), wf.name
