"""uv.lock must stay in step with pyproject.toml (2026-11-14).

Dependabot's `pip` ecosystem bumps the exact dev pins in pyproject.toml's `dev`/`selfheal`
extras (ruff 0.17.3 -> 0.17.4 in #2279, types-pyyaml in #2275) but does not rewrite uv.lock.
The lock then no longer matches the project, so CI's research audit (`uv export --locked`)
re-resolves instead of exporting, and that fresh resolve failed outright on a Pyodide fork
that torch has no wheels for. Both research-audit legs were red on main from 2ad0549 on.

These checks need no network and no uv: they read the two files and compare what uv itself
compares (the project's recorded `requires-dist`), so the drift fails the core suite on the
Dependabot PR instead of surfacing later as an audit failure. Fix a failure with `uv lock`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent
_EXTRA = re.compile(r"extra == '([^']+)'")


def _spec_key(specifier: str) -> frozenset[tuple[str, Version]]:
    # "==4.26" and "==4.26.0" are the same pin; uv writes the normalized form into the lock.
    if not specifier:
        return frozenset()
    return frozenset((s.operator, Version(s.version)) for s in SpecifierSet(specifier))


def _pyproject_requirements() -> set[tuple[str, str, frozenset]]:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wanted = set()
    for extra, entries in project.get("optional-dependencies", {}).items():
        for entry in entries:
            req = Requirement(entry)
            wanted.add((canonicalize_name(req.name), extra, _spec_key(str(req.specifier))))
    for entry in project.get("dependencies", []):
        req = Requirement(entry)
        wanted.add((canonicalize_name(req.name), "", _spec_key(str(req.specifier))))
    return wanted


def _lock() -> dict:
    return tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))


def _locked_requirements() -> set[tuple[str, str, frozenset]]:
    root = next(p for p in _lock()["package"] if p.get("source", {}).get("editable") == ".")
    recorded = set()
    for row in root["metadata"]["requires-dist"]:
        extra = _EXTRA.search(row.get("marker", ""))
        recorded.add((canonicalize_name(row["name"]), extra.group(1) if extra else "", _spec_key(row.get("specifier", ""))))
    return recorded


def test_uv_lock_records_the_requirements_pyproject_declares():
    wanted, recorded = _pyproject_requirements(), _locked_requirements()
    stale = sorted((name, extra, sorted(str(v) for _, v in spec)) for name, extra, spec in wanted - recorded)
    gone = sorted((name, extra, sorted(str(v) for _, v in spec)) for name, extra, spec in recorded - wanted)
    assert not stale and not gone, (
        "uv.lock is out of date with pyproject.toml, so `uv export --locked` (CI's research audit) "
        f"fails. Fix: run `uv lock` and commit uv.lock. In pyproject only: {stale}; in uv.lock only: {gone}"
    )


def test_uv_lock_has_no_pyodide_fork():
    # torch publishes no emscripten wheels and no sdist, so a lock that forks for Pyodide cannot
    # be re-resolved at all; pyproject's [tool.uv] environments excludes it.
    lock = _lock()
    markers = [*lock.get("resolution-markers", []), *lock.get("supported-markers", [])]
    assert not [m for m in markers if "sys_platform == 'emscripten'" in m], markers
