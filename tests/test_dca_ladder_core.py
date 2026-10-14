"""Tests for ``dca_ladder_core``: the ladder and base-selection arithmetic, extracted from
``near_close_dca`` so the plan-first backup executor can run it in the Perplexity sandbox.

Written test-first. Three promises:

1. It reproduces ``near_close_dca``'s results exactly (``tests/fixtures/ladder_golden.json``, frozen
   before the extraction; ``tests/test_ladder_characterization.py`` pins the same values against the
   live functions).
2. It is a single self-contained stdlib file: it imports nothing from this repo, so the file alone runs
   in a bare interpreter. That is what makes a small sandbox build possible.
3. ``near_close_dca`` delegates to it. There is one implementation, not a copy that can drift.

The extraction changes no behavior. The one fix it carries is main's ``55e0340``: a non-finite drop fails
closed to 0.0 on both ladders.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import dca_config
import dca_ladder_core as core
import near_close_dca

REPO = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "ladder_golden.json").read_bytes())


@pytest.fixture(scope="module")
def configs():
    live = dca_config.load_strategy_config(dca_config.get_config_dir())
    return {
        "live": live,
        "global": dataclasses.replace(live, per_ticker_sizing={}),
        "ml": dataclasses.replace(live, ml_sizing_adjustment={"VT": 1.2, "MSFT": 0.8, "AAPL": 1.0}),
        "kill_switch": dataclasses.replace(live, deployed_last_7d_usd=1e9),
        "inverse_martingale": dataclasses.replace(live, inverse_martingale={"VT": {"enabled": True}}),
    }


def _core_inputs(config, ticker):
    """What a caller hands the core: plain data pulled out of the config, no config object."""
    calibration = near_close_dca._ticker_sizing(ticker, config)
    return {
        "tiers": calibration.get("tiers", config.sizing_tiers),
        "interpolate": bool(calibration.get("interpolate")),
        "ml_factor": config.ml_sizing_adjustment.get(ticker.upper(), 1.0),
        "kill_switch": near_close_dca.kill_switch_engaged(config),
        "suppressed": bool(near_close_dca._inverse_martingale_config(ticker, config)),
    }


# ---------------------------------------------------------------------------
# 1. Parity with the frozen outputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", sorted(GOLDEN["multipliers"]))
def test_core_reproduces_every_raw_and_graded_multiplier(configs, scenario):
    config = configs[scenario]
    for ticker, expected in GOLDEN["multipliers"][scenario].items():
        inputs = _core_inputs(config, ticker)
        raw = [
            core.ladder_multiplier(d, inputs["tiers"], interpolate=inputs["interpolate"], suppressed=inputs["suppressed"])
            for d in GOLDEN["drops"]
        ]
        graded = [core.graded_multiplier(d, **inputs) for d in GOLDEN["drops"]]
        assert raw == expected["raw"], (scenario, ticker, "raw")
        assert graded == expected["graded"], (scenario, ticker, "graded")


def test_core_reproduces_every_base_selection(configs):
    for case in GOLDEN["bases"]:
        config = configs[case["config"]]
        calibration = near_close_dca._ticker_sizing(case["ticker"], config)
        base, reason = core.select_base_usd(
            case["decision"], case["close"], case["low"],
            full_base=calibration.get("base_amount_usd", config.base_buy_amount_usd),
            reduced_base=calibration.get("base_amount_below_close_usd", config.base_buy_amount_below_close_usd),
        )
        assert [base, reason] == [case["base"], case["reason"]], case


def test_bases_below_one_dollar_are_floored_to_one_dollar():
    """'A model must never produce a sub-dollar notional': the $1 floor applies before the multiplier."""
    assert core.select_base_usd(99.0, 100.0, 99.5, full_base=0.25, reduced_base=0.5) == (1.0, core.BASE_BELOW_CLOSE_AND_LOW)
    assert core.select_base_usd(99.0, 100.0, 98.0, full_base=0.25, reduced_base=0.5) == (1.0, core.BASE_BELOW_CLOSE_ONLY)


def test_the_reason_strings_match_the_values_near_close_dca_records():
    assert core.BASE_BELOW_CLOSE_AND_LOW == near_close_dca.BASE_BELOW_CLOSE_AND_LOW == "below_prior_close_and_low"
    assert core.BASE_BELOW_CLOSE_ONLY == near_close_dca.BASE_BELOW_CLOSE_ONLY == "below_prior_close_only"
    assert core.BASE_NOT_BELOW_CLOSE == near_close_dca.BASE_NOT_BELOW_CLOSE == "not_below_prior_close_no_buy"


def test_tier_edges_from_sizing_ladder_json_are_min_inclusive_and_max_exclusive():
    """Derived from config/sizing_ladder.json itself, independent of near_close_dca."""
    import math

    tiers = dca_config.load_sizing_tiers(dca_config.get_config_dir())["sizing_tiers"]
    ordered = sorted(tiers)
    for index, (threshold, multiplier) in enumerate(ordered):
        assert core.ladder_multiplier(threshold, tiers) == multiplier
        below = math.nextafter(threshold, 0.0)
        expected_below = ordered[index - 1][1] if index else 0.0
        assert core.ladder_multiplier(below, tiers) == expected_below
    assert core.ladder_multiplier(5.0, tiers) == ordered[-1][1]  # no upper cutoff


def test_a_non_finite_drop_fails_closed_on_both_ladders():
    """NaN and +/-inf must never fall through to the top tier (main's 55e0340, ported into the core)."""
    tiers = [(0.001, 1.0), (0.01, 2.0), (0.02, 10.0)]
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert core.interpolate_tier_multiplier(bad, tiers) == 0.0
        assert core.ladder_multiplier(bad, tiers, interpolate=True) == 0.0
        assert core.ladder_multiplier(bad, tiers) == 0.0
        assert core.graded_multiplier(bad, tiers, interpolate=True) == 0.0
        assert core.graded_multiplier(bad, tiers) == 0.0


def test_functions_are_pure_and_do_not_mutate_their_inputs():
    tiers = [(0.001, 1.0), (0.005, 1.5), (0.01, 2.0)]
    snapshot = list(tiers)
    first = core.graded_multiplier(0.006, tiers, interpolate=True)
    assert core.graded_multiplier(0.006, tiers, interpolate=True) == first
    assert tiers == snapshot  # unsorted input stays untouched; the core sorts a copy


def test_the_ceiling_clamp_and_kill_switch_cap_apply_in_that_order():
    tiers = [(0.001, 8.0)]
    assert core.graded_multiplier(0.01, tiers, ml_factor=1.5) == 10.0
    assert core.graded_multiplier(0.01, tiers, ml_factor=1.5, kill_switch=True) == 1.0
    assert core.graded_multiplier(0.0001, tiers, ml_factor=1.5, kill_switch=True) == 0.0  # no buy stays no buy
    assert core.graded_multiplier(0.01, tiers, suppressed=True) == 0.0


# ---------------------------------------------------------------------------
# 2. One small, self-contained stdlib file
# ---------------------------------------------------------------------------

_CORE = REPO / "dca_ladder_core.py"


def test_the_core_imports_only_the_standard_library():
    tree = ast.parse(_CORE.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would tie the core to the package"
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names), sorted(modules - set(sys.stdlib_module_names))


def test_the_core_does_no_io_and_reads_no_environment():
    source = _CORE.read_text()
    for forbidden in ("open(", "os.environ", "getenv", "subprocess", "socket", "eval(", "exec(", "__import__"):
        assert forbidden not in source, forbidden


def test_the_core_stays_small():
    assert len(_CORE.read_text().splitlines()) <= 250  # a bare-interpreter sandbox build must stay reviewable


def test_the_file_alone_runs_in_a_bare_interpreter(tmp_path):
    """Copied into an empty directory, with no repo on sys.path, it imports and computes."""
    (tmp_path / "dca_ladder_core.py").write_text(_CORE.read_text())
    # -I keeps user site and repo paths out but also drops the cwd from sys.path, so the empty temp
    # directory is added explicitly: the copy is then the only way the import can succeed.
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import dca_ladder_core as c; "
        "print(c.ladder_multiplier(0.02, [(0.001, 1.0), (0.01, 2.0)]))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code, str(tmp_path)], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "2.0"


# ---------------------------------------------------------------------------
# 3. near_close_dca delegates: one implementation
# ---------------------------------------------------------------------------


def test_near_close_dca_reuses_the_cores_interpolation_function():
    assert near_close_dca.interpolate_tier_multiplier is core.interpolate_tier_multiplier


def test_graded_buy_multiplier_delegates_to_the_core(configs, monkeypatch):
    calls = []

    def spy(drop, tiers, **kwargs):
        calls.append((drop, kwargs))
        return 4.2

    monkeypatch.setattr(core, "graded_multiplier", spy)
    assert near_close_dca.graded_buy_multiplier(0.02, configs["ml"], "VT") == 4.2
    (drop, kwargs), = calls
    assert drop == 0.02
    assert kwargs["ml_factor"] == 1.2 and kwargs["kill_switch"] is False and kwargs["suppressed"] is False


def test_raw_ladder_multiplier_delegates_to_the_core(configs, monkeypatch):
    seen = []
    monkeypatch.setattr(core, "ladder_multiplier", lambda drop, tiers, **kw: seen.append(kw) or 3.3)
    assert near_close_dca.raw_ladder_multiplier(0.02, configs["live"], "VT") == 3.3
    assert seen and seen[0]["interpolate"] is True  # VT's per-ticker curve is interpolated


def test_select_first_allocation_base_delegates_to_the_core(configs, monkeypatch):
    seen = []
    monkeypatch.setattr(core, "select_base_usd", lambda *a, **kw: seen.append(kw) or (7.0, "stub"))
    assert near_close_dca.select_first_allocation_base_usd(99.0, 100.0, 98.0, configs["global"], "VT") == (7.0, "stub")
    assert seen[0]["full_base"] == 2.0 and seen[0]["reduced_base"] == 1.0


def test_the_module_is_listed_for_packaging():
    packaging = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert "dca_ladder_core" in packaging["tool"]["setuptools"]["py-modules"]
