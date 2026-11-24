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
# overnight_futures/ joins them under docs/adr-overnight-futures-tracker.md (D3).
# factor_signals/ joins them under docs/adr-factor-residual-dip-signals.md.
_OWN_PACKAGES = {"connector_signals", "market_data_ingest", "comovement_signals", "overnight_futures",
                 "factor_signals", "inventory_signals", "mc_research", "residual_reversal",
                 "cost_basis_backtest"}

# package directory name -> names it must never import
FORBIDDEN_IMPORTS: dict[str, set[str]] = {
    # comovement_signals is forbidden to every other package too (review round 1 on
    # PR #2106): independence must hold both ways, so only SizingAdjustmentReader may ever
    # know it exists. overnight_futures gets the same two-way rule
    # (docs/adr-overnight-futures-tracker.md, D3) -- and unlike comovement_signals, not even
    # SizingAdjustmentReader may learn it exists (D2), which the core-side test below pins.
    "ml_forecaster": {"wondrous_octopus", "connector_signals", "comovement_signals", "overnight_futures",
                      "factor_signals", "inventory_signals", "residual_reversal", "cost_basis_backtest",
                      "near_close_dca"},
    "wondrous_octopus": {"ml_forecaster", "connector_signals", "comovement_signals", "overnight_futures",
                         "factor_signals", "inventory_signals", "residual_reversal", "cost_basis_backtest",
                         "near_close_dca"},
    "connector_signals": {"ml_forecaster", "wondrous_octopus", "comovement_signals", "overnight_futures",
                          "factor_signals", "inventory_signals", "residual_reversal", "cost_basis_backtest",
                          "near_close_dca"},
    "market_data_ingest": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                           "overnight_futures", "factor_signals", "inventory_signals", "residual_reversal",
                           "cost_basis_backtest", "near_close_dca"},
    # Pure co-movement signals (docs/adr-comovement-signals.md): prices arrive as rows from
    # the caller, so it needs neither the ingest layer nor any other suggester.
    "comovement_signals": {"ml_forecaster", "wondrous_octopus", "connector_signals",
                           "market_data_ingest", "overnight_futures", "factor_signals", "inventory_signals",
                           "residual_reversal", "cost_basis_backtest", "near_close_dca"},
    # Pure overnight gap computation (docs/adr-overnight-futures-tracker.md, D3): rows arrive
    # from the caller, exactly as in comovement_signals, so it needs neither the ingest layer
    # (market_data_ingest/futures/ writes the store; this package never reads it directly) nor
    # any suggester. dca_shared_interfaces is the only sibling it may import.
    "overnight_futures": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                          "market_data_ingest", "factor_signals", "inventory_signals", "residual_reversal",
                          "cost_basis_backtest", "near_close_dca"},
    # Pure factor decomposition (docs/adr-factor-residual-dip-signals.md): rows arrive from
    # the caller, as in comovement_signals, and the two-way rule applies. It may not import
    # comovement_signals even to reuse its returns contract -- it carries its own copy.
    "factor_signals": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                       "market_data_ingest", "overnight_futures", "inventory_signals", "mc_research",
                       "residual_reversal", "cost_basis_backtest", "near_close_dca"},
    # Inventory reservation (docs/inventory_reservation_suggester.md): same two-way rule as
    # comovement_signals. Inputs arrive from the caller, so it needs no ingest layer and no
    # other suggester; only SizingAdjustmentReader may ever learn it exists.
    "inventory_signals": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                          "market_data_ingest", "overnight_futures", "factor_signals", "mc_research",
                          "residual_reversal", "cost_basis_backtest", "near_close_dca"},
    # Research-only Monte Carlo harness (docs/mc_variance_reduction.md): paths are simulated
    # in-package and strategies take tiers as arguments, so it needs no suggester, no ingest
    # layer and no live module. Stdlib-only is pinned separately in tests/mc_research/.
    "mc_research": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                    "market_data_ingest", "overnight_futures", "inventory_signals", "factor_signals", "near_close_dca",
                    "sizing_adjustment_reader", "dca_ladder_core", "dca_runner", "run_dca", "dca_live_adapters",
                    "residual_reversal", "cost_basis_backtest"},
    # Sector-ETF residual reversal (docs/adr-residual-reversal-suggester.md): rows arrive
    # from the caller, so the same two-way rule as comovement_signals applies. It carries its
    # own OLS/OU math rather than reusing factor_signals' betas.
    "residual_reversal": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                          "market_data_ingest", "overnight_futures", "factor_signals", "inventory_signals",
                          "mc_research", "cost_basis_backtest", "near_close_dca"},
    # Offline cost-basis backtest harness (docs/adr-cost-basis-backtest-harness.md): it replays
    # a candidate through duck typing and a caller-supplied factory, so it imports no
    # suggester, and it reads the Phase 0 ledger only as plain dict rows, so it does not
    # import market_data_ingest either. Two-way: no other package may import it.
    "cost_basis_backtest": {"ml_forecaster", "wondrous_octopus", "connector_signals", "comovement_signals",
                            "market_data_ingest", "overnight_futures", "factor_signals", "inventory_signals",
                            "mc_research", "residual_reversal", "near_close_dca"},
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
                 "market_data_ingest", "comovement_signals", "overnight_futures", "factor_signals",
                 "inventory_signals", "residual_reversal", "cost_basis_backtest",
                 "near_close_dca"}
    violations = []
    for py_file in shared.rglob("*.py"):
        hit = _imported_module_names(py_file) & forbidden
        if hit:
            violations.append(f"{py_file.relative_to(this_package_root)} imports forbidden {sorted(hit)}")
    assert not violations, "dca_shared_interfaces boundary violated:\n" + "\n".join(violations)


# --- overnight_futures (docs/adr-overnight-futures-tracker.md) -------------------------------
#
# test_no_forbidden_cross_imports skips a package it cannot find, so registering
# overnight_futures above would pass vacuously while the package does not exist. The tests
# below make the registration bite: the package must be on disk, the two-way rule must be
# complete, the live side must never import it, and it must never grow a sizing path.

_OVERNIGHT = "overnight_futures"


def test_overnight_futures_package_exists() -> None:
    """D3: the package ships inside dca-sizing-signals/, beside comovement_signals/."""
    package_dir = _resolve_package_dir(_OVERNIGHT)
    assert package_dir is not None, f"{_OVERNIGHT}/ not found under {PACKAGE_ROOT}"
    assert package_dir.parent == PACKAGE_ROOT, f"{_OVERNIGHT}/ must live in {PACKAGE_ROOT}, not {package_dir}"
    assert (package_dir / "__init__.py").is_file(), f"{_OVERNIGHT}/__init__.py is missing"


def test_overnight_futures_is_forbidden_to_every_other_package() -> None:
    """D3: independence holds both ways. A new sibling added to FORBIDDEN_IMPORTS later
    without overnight_futures in its set fails here, not silently."""
    missing = sorted(pkg for pkg, forbidden in FORBIDDEN_IMPORTS.items()
                     if pkg != _OVERNIGHT and _OVERNIGHT not in forbidden)
    assert not missing, f"{_OVERNIGHT} is not forbidden to: {missing}"


def test_live_side_never_imports_overnight_futures() -> None:
    """D2: no repo-root module -- the core, the three entry points and, unlike every other
    suggester, sizing_adjustment_reader.py -- may import overnight_futures. The ADR's
    verification step ("a grep shows no import ... from the core or from
    sizing_adjustment_reader.py"), as a test rather than a one-off grep."""
    root_modules = sorted(REPO_ROOT.glob("*.py"))
    assert (REPO_ROOT / "sizing_adjustment_reader.py") in root_modules, (
        f"sizing_adjustment_reader.py not found at REPO_ROOT={REPO_ROOT}; point DCA_REPO_ROOT at the repo root"
    )
    violations = [str(_display_path(f)) for f in root_modules if _OVERNIGHT in _imported_module_names(f)]
    assert not violations, f"live-side modules import {_OVERNIGHT}: {violations}"


def test_overnight_futures_has_no_sizing_path() -> None:
    """D2: a ForecastProvider only. It must never import SizingSuggester or
    SuggestionPublisher, and never define suggest()/publish() -- so it cannot satisfy the
    SizingSuggester Protocol or write a suggestion file even by accident. Wiring it into
    sizing needs its own later ADR (D8)."""
    package_dir = _resolve_package_dir(_OVERNIGHT)
    assert package_dir is not None, f"{_OVERNIGHT}/ not found under {PACKAGE_ROOT}"
    banned_names = {"SizingSuggester", "SuggestionPublisher", "SizingSuggestion"}
    banned_defs = {"suggest", "publish"}
    violations: list[str] = []
    for py_file in package_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                hit = {alias.name for alias in node.names} & banned_names
                if hit:
                    violations.append(f"{_display_path(py_file)} imports {sorted(hit)}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in banned_defs:
                violations.append(f"{_display_path(py_file)}:{node.lineno} defines {node.name}()")
            elif isinstance(node, ast.Attribute) and node.attr in banned_names:
                violations.append(f"{_display_path(py_file)}:{node.lineno} references {node.attr}")
    assert not violations, f"{_OVERNIGHT} grew a sizing path:\n" + "\n".join(violations)


def test_overnight_futures_is_packaged() -> None:
    """Same lesson as the root pyproject's py-modules trail: a package missing from
    packages.find works from a checkout and ImportErrors from an installed wheel."""
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text()
    assert f'"{_OVERNIGHT}*"' in pyproject, f'add "{_OVERNIGHT}*" to [tool.setuptools.packages.find] include'


# --- factor_signals (docs/adr-factor-residual-dip-signals.md) --------------------------------
#
# Same reason as the overnight_futures block above: a registration that silently skips a
# missing package proves nothing, so these make it bite.

_FACTOR = "factor_signals"


def test_factor_signals_package_exists() -> None:
    package_dir = _resolve_package_dir(_FACTOR)
    assert package_dir is not None, f"{_FACTOR}/ not found under {PACKAGE_ROOT}"
    assert package_dir.parent == PACKAGE_ROOT
    assert (package_dir / "__init__.py").is_file()


def test_factor_signals_is_forbidden_to_every_other_package() -> None:
    missing = sorted(pkg for pkg, forbidden in FORBIDDEN_IMPORTS.items()
                     if pkg != _FACTOR and _FACTOR not in forbidden)
    assert not missing, f"{_FACTOR} is not forbidden to: {missing}"


# --- mc_research (docs/mc_variance_reduction.md) ----------------------------------------------
#
# Same shape as the overnight_futures block: registration alone would pass vacuously if the
# package vanished, so pin existence, packaging, and that nothing live ever imports it.

_MC = "mc_research"


def test_mc_research_package_exists_and_is_packaged() -> None:
    package_dir = _resolve_package_dir(_MC)
    assert package_dir is not None and package_dir.parent == PACKAGE_ROOT, f"{_MC}/ must live in {PACKAGE_ROOT}"
    assert (package_dir / "__init__.py").is_file(), f"{_MC}/__init__.py is missing"
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text()
    assert f'"{_MC}*"' in pyproject, f'add "{_MC}*" to [tool.setuptools.packages.find] include'


def test_live_side_never_imports_mc_research() -> None:
    """Simulation results are research evidence only: no repo-root module (the core, the three
    entry points, sizing_adjustment_reader.py) may import the harness."""
    root_modules = sorted(REPO_ROOT.glob("*.py"))
    assert (REPO_ROOT / "sizing_adjustment_reader.py") in root_modules, (
        f"sizing_adjustment_reader.py not found at REPO_ROOT={REPO_ROOT}; point DCA_REPO_ROOT at the repo root"
    )
    violations = [str(_display_path(f)) for f in root_modules if _MC in _imported_module_names(f)]
    assert not violations, f"live-side modules import {_MC}: {violations}"
