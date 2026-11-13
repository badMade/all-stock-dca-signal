"""requirements.txt and pyproject.toml's ``prices`` extra must pin the same versions.

pyproject.toml says the extra uses "Same pins as requirements.txt", but nothing enforced
it: a 2026-10-27 bump moved duckdb 1.5.6 -> 2.0.0 in requirements.txt only, so CI (which
installs requirements.txt) tested 2.0.0 while ``pip install -e ".[prices]"`` still gave
1.5.6. Found 2026-11-12 during PR #2242; this test keeps the two from drifting again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11; this package allows 3.10

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_EXACT_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)")


def _normalize(name: str) -> str:
    # PEP 503: names compare case-insensitively with runs of -, _ and . treated alike.
    return re.sub(r"[-_.]+", "-", name).lower()


def _exact_pins(lines: list[str]) -> dict[str, str]:
    pins = {}
    for line in lines:
        match = _EXACT_PIN.match(line)
        if match:
            pins[_normalize(match.group(1))] = match.group(2)
    return pins


def test_prices_extra_matches_requirements_txt():
    pyproject = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extra = _exact_pins(pyproject["project"]["optional-dependencies"]["prices"])
    requirements = _exact_pins((PACKAGE_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())

    assert extra, "the prices extra pins nothing; this test would pass vacuously"
    mismatched = {
        name: {"pyproject prices extra": version, "requirements.txt": requirements.get(name)}
        for name, version in extra.items()
        if requirements.get(name) != version
    }
    assert not mismatched, f"pins differ between pyproject.toml and requirements.txt: {mismatched}"
