"""Enforces, at test level rather than by convention alone, that
independent forecasting/suggestion components never import each other or
the live DCA engine, and that the live engine never imports a concrete
suggester directly -- only dca_shared_interfaces.

Repo-root note, fixed during packaging: the original version of this test
assumed REPO_ROOT was two parents up from this file, which only held
under the flat sandbox layout it was first written in. Nested properly
here (dca-sizing-signals/tests/test_architecture_boundaries.py), that
same assumption would resolve to the wrong directory and silently check
nothing -- worse than failing loudly. This version instead:

  1. Reads DCA_REPO_ROOT from the environment if set (point this at the
     real all-stock-dca repo root), falling back to this package's own
     parent directory otherwise -- which, nested here, IS that repo root.
  2. Resolves each logical (import-name) package to its real directory,
     handling two facts of the actual all-stock-dca layout that the flat
     sandbox never had: the live siblings are checked out under their
     hyphenated repo names (ml-forecaster/, wondrous-octopus/), not the
     ml_forecaster/ wondrous_octopus/ import names; and connector_signals/
     and market_data_ingest/ ship INSIDE this package, one level below the
     repo root, so they resolve against this package's own root. Only if
     the two live siblings genuinely can't be found does it WARN rather
     than silently pass -- that path now means a real misconfiguration,
     not the expected pre-placement state it once described.
  3. Always runs one check that needs no configuration at all: nothing
     inside dca_shared_interfaces/ imports anything it shouldn't,
     checked relative to this package's own known layout.

Stdlib only (ast + pathlib + os + warnings), no new dependency.
"""

from __future__ import annotations

import ast
import os
import warnings
from pathlib import Path

REPO_ROOT = Path(os.environ.get("DCA_REPO_ROOT", str(Path(__file__).resolve().parents[2])))
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# connector_signals/ and market_data_ingest/ ship inside THIS package, so they
# resolve against the package root rather than the wider repo root.
_OWN_PACKAGES = {"connector_signals", "market_data_ingest", "comovement_signals"}

# package directory name -> names it must never import
FORBIDDEN_IMPORTS: dict[str, set[str]] = {
    # comovement_signals is forbidden to every other package too (review round 1 on
    # PR #2106): independence must hold both ways, so only SizingAdjustmentReader may ever
    # know it exists.
    "ml_forecaster": {"wondrous_octopus", "connector_signals", "comovement_signals", "near_close_dca"},
    "wondrous_octopus": {"ml_forecaster", "connector_signals", "comovement_signals", "near_close_dca"},
    "connector_signals": {"ml_forecaster", "wondrous_octopus", "comovement_signals", "near_close_dca"},
    "market_data_ingest": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                           "near_close_dca"},
    # Pure co-movement signals (docs/adr-comovement-signals.md): prices arrive as rows from
    # the caller, so it needs neither the ingest layer nor any other suggester.
    "comovement_signals": {"ml_forecaster", "wondrous_octopus", "connector_signals",
                           "market_data_ingest", "near_close_dca"},
}


def _resolve_package_dir(name: str) -> Path | None:
    """Map a logical (import-name) package to its real directory, or None.

    Two wrinkles of the real all-stock-dca layout, neither present in the flat
    sandbox this test began in:

      * connector_signals/ and market_data_ingest/ live inside this package --
        resolve them against PACKAGE_ROOT, not the repo root.
      * the live siblings are checked out as ml-forecaster/ and wondrous-octopus/
        (their hyphenated repo/directory names), while the boundary rules key off
        the ml_forecaster/ wondrous_octopus/ import names. Try the import name
        first, then the hyphenated directory name.
    """
    if name in _OWN_PACKAGES:
        candidate = PACKAGE_ROOT / name
        return candidate if candidate.exists() else None
    for dirname in (name, name.replace("_", "-")):
        candidate = REPO_ROOT / dirname
        if candidate.exists():
            return candidate
    return None


def _display_path(py_file: Path) -> Path:
    """Prefer a repo-root-relative path in messages, but never crash if a
    resolved package (e.g. a nested own-package) sits outside REPO_ROOT."""
    try:
        return py_file.relative_to(REPO_ROOT)
    except ValueError:
        return py_file


def _imported_module_names(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_forbidden_cross_imports() -> None:
    """Fails with a specific file and forbidden name on any violation.
    A package not found on disk is skipped, not failed -- see the module
    docstring for how each is resolved and when a miss instead warns."""
    violations: list[str] = []
    found_ml_forecaster_or_octopus = False
    for package, forbidden in FORBIDDEN_IMPORTS.items():
        package_dir = _resolve_package_dir(package)
        if package_dir is None:
            continue
        if package in ("ml_forecaster", "wondrous_octopus"):
            found_ml_forecaster_or_octopus = True
        for py_file in package_dir.rglob("*.py"):
            hit = _imported_module_names(py_file) & forbidden
            if hit:
                violations.append(f"{_display_path(py_file)} imports forbidden {sorted(hit)}")
    assert not violations, "Architecture boundary violated:\n" + "\n".join(violations)
    if not found_ml_forecaster_or_octopus:
        warnings.warn(
            f"ml_forecaster/ and wondrous_octopus/ were not found from REPO_ROOT={REPO_ROOT} "
            "(checked both the import names and the hyphenated ml-forecaster/ "
            "wondrous-octopus/ directory names) -- the cross-subproject check ran "
            "against whatever packages ARE present. Point DCA_REPO_ROOT at the real "
            "all-stock-dca repo root.",
            stacklevel=1,
        )


def test_dca_shared_interfaces_has_no_forbidden_imports() -> None:
    """Meaningful right now with no configuration: the one package
    guaranteed to be on disk relative to this test file regardless of
    where the wider repo puts everything else."""
    this_package_root = Path(__file__).resolve().parents[1]
    shared = this_package_root / "dca_shared_interfaces"
    assert shared.exists(), "dca_shared_interfaces/ not found next to tests/ -- check package layout"
    forbidden = {"ml_forecaster", "wondrous_octopus", "connector_signals",
                 "market_data_ingest", "comovement_signals", "near_close_dca"}
    violations = []
    for py_file in shared.rglob("*.py"):
        hit = _imported_module_names(py_file) & forbidden
        if hit:
            violations.append(f"{py_file.relative_to(this_package_root)} imports forbidden {sorted(hit)}")
    assert not violations, "dca_shared_interfaces boundary violated:\n" + "\n".join(violations)
