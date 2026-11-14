""".github/dependabot.yml: only the `uv` job may bump a package uv.lock records (2026-11-14).

Both root jobs edit pyproject.toml. The `uv` job moves pyproject.toml, requirements-dev.txt and
uv.lock together (#2145 ruff 0.17.3, #2211 uv 0.13.7); the `pip` job moved the same exact pins in
pyproject.toml and requirements-dev.txt but never uv.lock (#2279 ruff 0.17.4, #2275 types-pyyaml).
That left `uv export --locked` re-resolving, and both research-audit legs were red on main until
#2296 relocked. So the `pip` job ignores every package pyproject.toml declares, EXCEPT the ones a
sibling manifest also lists: ignoring those would freeze the sibling's pin too (ignore cannot be
scoped to a path), and a drift they cause still fails tests/test_uv_lock_sync.py on the PR.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parent.parent


def _pyproject_packages() -> set[str]:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    entries = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        entries.extend(extra)
    return {canonicalize_name(Requirement(entry).name) for entry in entries}


def _sibling_manifest_packages() -> set[str]:
    # Every requirements file except the root dev manifest, which the uv job maintains.
    names: set[str] = set()
    for path in REPO.rglob("requirements*.txt"):
        rel = path.relative_to(REPO)
        if rel.parts[0] in {".venv", "node_modules"} or rel == Path("requirements-dev.txt"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            try:
                names.add(canonicalize_name(Requirement(line).name))
            except InvalidRequirement:
                continue
    return names


def _root_job(ecosystem: str) -> dict:
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    jobs = [u for u in config["updates"] if u["package-ecosystem"] == ecosystem and u.get("directory") == "/"]
    assert len(jobs) == 1, f"expected one root {ecosystem} job, found {len(jobs)}"
    return jobs[0]


def _pip_ignored() -> set[str]:
    return {canonicalize_name(row["dependency-name"]) for row in _root_job("pip").get("ignore", [])}


def test_pip_job_leaves_lock_recorded_packages_to_the_uv_job():
    _root_job("uv")  # the job that does relock must still exist
    expected = _pyproject_packages() - _sibling_manifest_packages()
    missing = sorted(expected - _pip_ignored())
    assert not missing, (
        "the root pip Dependabot job would bump these pyproject.toml pins without relocking uv.lock; "
        f"add them to its `ignore` list in .github/dependabot.yml: {missing}"
    )


def test_pip_job_does_not_freeze_sibling_manifest_pins():
    frozen = sorted(_pip_ignored() & _sibling_manifest_packages())
    assert not frozen, (
        "these are also listed in a sibling requirements file, and Dependabot's ignore cannot be "
        f"scoped to a path, so ignoring them stops the sibling's updates too: {frozen}"
    )
    stale = sorted(_pip_ignored() - _pyproject_packages())
    assert not stale, f"the pip job ignores packages pyproject.toml no longer declares: {stale}"
