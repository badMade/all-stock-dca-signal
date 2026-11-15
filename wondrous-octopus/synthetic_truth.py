#!/usr/bin/env python3
"""Synthetic-truth recovery: does the harness report an answer known in advance?

    python wondrous-octopus/synthetic_truth.py --scenario all --work-dir /tmp/octopus-truth

Every other tool here measures the model. This one measures the measuring
instrument. It writes small price archives whose answer is fixed by
construction, runs them through the UNMODIFIED walk-forward harness
(``run_walkforward.main``, the same entry point a real replay uses), and
checks what comes out:

==================  ==========  ===============================================
scenario            must show   because
==================  ==========  ===============================================
``random_walk``     no skill    nothing is predictable, so any skill reported
                                is a lookahead leak or a scoring bug
``garch_leverage``  no skill    the SIZE of tomorrow's move is predictable, its
                                direction still is not -- volatility structure
                                must not be mistaken for point skill
``planted_drift``   skill       returns follow yesterday's with a known
                                strength; a harness that cannot see this has no
                                power, and its "no skill" elsewhere means nothing
==================  ==========  ===============================================

The simulators are not claimed to resemble real markets. They are claimed to
have a known answer.

Skill is measured against the no-change forecast (tomorrow's close equals
today's): ``1 - sum|predicted - actual| / sum|actual|`` in close log-return
space. That is what the persistence model predicts, but computing it from
the forecaster's own score records needs no second model run and is aligned
record for record by construction.

The "no skill" check is one-sided. A fitted model on pure noise fits some of
the noise and comes out slightly WORSE than doing nothing; that is honest
behaviour, not a harness fault. Only skill that should not exist fails.

Exit status: 0 when every scenario matches its expectation, 1 otherwise --
here, unlike the trust scorecard, a failed check IS a failure of the tool
under test and should stop whatever depends on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from collections import defaultdict  # noqa: E402
from typing import Any, Iterable, Mapping  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from band_uncertainty import Interval, ratio_interval, skill_interval  # noqa: E402

#: Scenario -> what an honest harness must report for it.
SCENARIOS: dict[str, str] = {
    "random_walk": "no_skill",
    "garch_leverage": "no_skill",
    "planted_drift": "skill",
}

#: Daily volatility of the simulated log-returns: an ordinary single-stock
#: figure, chosen so bars stay well inside the data layer's geometry checks.
DAILY_VOL = 0.015

#: Default lag-1 correlation of the planted drift. Strong on purpose: this
#: scenario tests whether the harness has ANY power, on a run short enough
#: to finish in minutes. A subtler signal needs a longer archive.
DEFAULT_STRENGTH = 0.5

#: GARCH(1,1) with a leverage term (the GJR form): tomorrow's variance is
#: ``omega + BETA * variance + ALPHA * r^2 + LEVERAGE * r^2 * [r < 0]``.
#: ALPHA + LEVERAGE / 2 + BETA = 0.95, so the process is stationary with
#: unconditional variance DAILY_VOL^2.
GARCH_BETA = 0.85
GARCH_ALPHA = 0.03
GARCH_LEVERAGE = 0.14

#: First archive date and first PREDICTION date. The gap is the warm-up the
#: harness needs before any model may be scored (its --min-history is 60).
ARCHIVE_START = "2018-01-01"
PREDICTION_START = "2018-06-01"

#: Bootstrap block for the verdict's intervals. Horizon is 1 throughout; the
#: block covers day-to-day dependence the same way the trust scorecard does.
BLOCK_LENGTH = 5

RESULT_FILENAME = "synthetic_truth.json"


def simulate_returns(scenario: str, *, n_rows: int, seed: int, strength: float = DEFAULT_STRENGTH) -> np.ndarray:
    """Daily close log-returns for one ticker under ``scenario``."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {sorted(SCENARIOS)}")
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0.0, 1.0, size=n_rows)
    if scenario == "random_walk":
        return DAILY_VOL * shocks
    returns = np.zeros(n_rows)
    if scenario == "planted_drift":
        # AR(1): today's return is `strength` times yesterday's plus news.
        for row in range(1, n_rows):
            returns[row] = strength * returns[row - 1] + DAILY_VOL * shocks[row]
        return returns
    variance = DAILY_VOL**2
    omega = variance * (1.0 - GARCH_BETA - GARCH_ALPHA - GARCH_LEVERAGE / 2.0)
    for row in range(n_rows):
        returns[row] = math.sqrt(variance) * shocks[row]
        squared = returns[row] ** 2
        variance = omega + GARCH_BETA * variance + GARCH_ALPHA * squared
        if returns[row] < 0.0:
            variance += GARCH_LEVERAGE * squared
    return returns


def expected_hit_rate(strength: float) -> float:
    """Chance that yesterday's sign predicts today's, for lag-1 correlation ``strength``.

    Sheppard's formula for a bivariate normal: ``1/2 + asin(rho) / pi``. The
    ceiling a sign-following forecaster can reach on the planted drift.
    """
    return 0.5 + math.asin(strength) / math.pi


def price_frame(returns: np.ndarray, *, seed: int, start: str = ARCHIVE_START) -> pd.DataFrame:
    """One ticker's OHLCV frame in the archive's Yahoo layout.

    ``Close`` carries the simulated log-returns exactly -- the harness scores
    close log-returns, so anything else would change the known answer. Open,
    High and Low are built around it with small noise, consistent by
    construction. No splits or dividends, so ``Adj Close`` equals ``Close``.
    """
    # A separate stream from the returns' own, so bar noise cannot correlate
    # with the series whose predictability is under test.
    rng = np.random.default_rng([seed, 1])
    n_rows = len(returns)
    close = 50.0 * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[close[0]], close[:-1]]) * np.exp(rng.normal(0.0, 0.002, size=n_rows))
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.008, size=n_rows))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.008, size=n_rows))
    return pd.DataFrame(
        {
            "Date": pd.bdate_range(start=start, periods=n_rows),
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Adj Close": close,
            "Volume": rng.integers(500_000, 5_000_000, size=n_rows),
            "Dividends": np.zeros(n_rows),
            "Stock Splits": np.zeros(n_rows),
        }
    )


def _ticker_symbol(index: int) -> str:
    # Letters only: data._TICKER_SYMBOL_PATTERN accepts [A-Z.]{1,6}, and an
    # archive of "S00.csv" files is silently discovered as EMPTY -- a check
    # that then "passes" on zero forecasts. Two letters give 676 symbols.
    return "SY" + chr(65 + index // 26) + chr(65 + index % 26)


def write_archive(
    directory: Path, scenario: str, *, n_tickers: int, n_rows: int, seed: int, strength: float = DEFAULT_STRENGTH
) -> list[str]:
    """Write ``n_tickers`` independent series as ``<SYMBOL>.csv``; return the symbols."""
    directory.mkdir(parents=True, exist_ok=True)
    tickers: list[str] = []
    for index in range(n_tickers):
        # Independent across tickers on purpose (no shared market factor):
        # every ticker-day is then separate evidence about the known answer.
        ticker_seed = seed * 1000 + index
        returns = simulate_returns(scenario, n_rows=n_rows, seed=ticker_seed, strength=strength)
        ticker = _ticker_symbol(index)
        price_frame(returns, seed=ticker_seed).to_csv(directory / f"{ticker}.csv", index=False)
        tickers.append(ticker)
    return tickers


def evaluate(records: Iterable[Mapping[str, Any]], *, model: str) -> dict[str, Any]:
    """Skill, directional hit rate and coverage for ``model``'s close forecasts.

    Intervals are date-block bootstraps (``band_uncertainty``). A forecast of
    exactly zero, or a day that did not move, has no direction and is left
    out of the hit rate rather than counted as a miss.
    """
    # Per target date: forecasts, |error|, |actual|, directional calls, hits, in-band.
    days: dict[str, list[float]] = defaultdict(lambda: [0.0] * 6)
    for record in records:
        if record["model"] != model:
            continue
        score = record["score"]
        predicted = score["pred_close"]
        actual = score["actual_close"]
        row = days[record["target_date"]]
        row[0] += 1.0
        row[1] += abs(predicted - actual)
        row[2] += abs(actual)
        if predicted != 0.0 and actual != 0.0:
            row[3] += 1.0
            row[4] += 1.0 if (predicted > 0.0) == (actual > 0.0) else 0.0
        row[5] += 1.0 if score["in_band"]["close"] else 0.0
    if not days:
        empty = Interval(None, None, None, 0, BLOCK_LENGTH, "no_observations")
        return {"skill": empty, "hit_rate": empty, "coverage": empty, "n_dates": 0, "n_forecasts": 0}
    table = np.array([days[date] for date in sorted(days)], dtype=np.float64)
    return {
        "skill": skill_interval(table[:, 1], table[:, 2], block_length=BLOCK_LENGTH),
        "hit_rate": ratio_interval(table[:, 4], table[:, 3], block_length=BLOCK_LENGTH),
        "coverage": ratio_interval(table[:, 5], table[:, 0], block_length=BLOCK_LENGTH),
        "n_dates": len(table),
        "n_forecasts": int(table[:, 0].sum()),
    }


def verdict(evaluation: Mapping[str, Any], *, expect: str) -> dict[str, Any]:
    """Does the evaluation match what an honest harness must report?

    Fails closed: without an interval on skill there is nothing to sign off,
    for either expectation. A hit rate with no directional calls at all is
    different -- the forecaster claimed no direction, so there is no
    directional skill to be spurious.
    """
    if expect not in ("no_skill", "skill"):
        raise ValueError(f"expect must be 'no_skill' or 'skill', got {expect!r}")
    skill: Interval = evaluation["skill"]
    hit_rate: Interval = evaluation["hit_rate"]
    if skill.low is None:
        return {"passed": False, "reasons": [skill.reason or "interval_unavailable"]}
    reasons: list[str] = []
    if expect == "no_skill":
        if skill.low > 0.0:
            reasons.append("spurious_skill")
        # Checked separately from skill: a forecast with the right sign and
        # an absurd size is worse than no-change on error, yet has plainly
        # seen something it should not have.
        if hit_rate.low is not None and hit_rate.low > 0.5:
            reasons.append("spurious_direction")
    elif not (skill.low > 0.0 and hit_rate.low is not None and hit_rate.low > 0.5):
        reasons.append("signal_not_detected")
    return {"passed": not reasons, "reasons": reasons}


def _interval_payload(interval: Interval) -> dict[str, Any]:
    return {"point": interval.point, "low": interval.low, "high": interval.high, "reason": interval.reason}


def run_scenario(
    scenario: str,
    *,
    work_dir: Path,
    n_tickers: int = 8,
    n_rows: int = 500,
    seed: int = 0,
    strength: float = DEFAULT_STRENGTH,
) -> dict[str, Any]:
    """Write the archive, replay it through the real harness, return the verdict."""
    # Imported here, not at module top: these pull in the model stack, which
    # the simulators and the verdict (and their tests) do not need.
    import run_walkforward
    from inspect_run import _open_ledger

    expect = SCENARIOS.get(scenario)
    if expect is None:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {sorted(SCENARIOS)}")
    history_dir = Path(work_dir) / scenario / "archive"
    run_dir = Path(work_dir) / scenario / "run"
    write_archive(history_dir, scenario, n_tickers=n_tickers, n_rows=n_rows, seed=seed, strength=strength)
    # The gradient-boosted quantile model, not persistence: persistence
    # forecasts no change by definition, so it can neither leak nor detect.
    code = run_walkforward.main(
        [
            "--history-dir",
            str(history_dir),
            "--all-tickers-in-archive",
            "--model",
            "gbm",
            "--start",
            PREDICTION_START,
            "--run-dir",
            str(run_dir),
            "--seed",
            str(seed),
        ]
    )
    if code != 0:
        # The harness refused or failed. That is not "no skill found" -- it
        # is no evidence at all, and must not read as a pass.
        return {
            "scenario": scenario,
            "expect": expect,
            "n_dates": 0,
            "n_forecasts": 0,
            "verdict": {"passed": False, "reasons": [f"harness_exit_{code}"]},
        }
    ledger = _open_ledger(run_dir)
    evaluation = evaluate(ledger.iter_records(model="gbm_quantile", kind="score"), model="gbm_quantile")
    return {
        "scenario": scenario,
        "expect": expect,
        "n_tickers": n_tickers,
        "n_rows": n_rows,
        "seed": seed,
        "strength": strength if scenario == "planted_drift" else None,
        "n_dates": evaluation["n_dates"],
        "n_forecasts": evaluation["n_forecasts"],
        "skill": _interval_payload(evaluation["skill"]),
        "hit_rate": _interval_payload(evaluation["hit_rate"]),
        "coverage": _interval_payload(evaluation["coverage"]),
        "verdict": verdict(evaluation, expect=expect),
    }


def _format(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1%}"


def render_results(results: list[dict[str, Any]]) -> str:
    header = ("scenario", "expect", "forecasts", "skill (95% interval)", "hit rate")
    lines = [f"{header[0]:<16} {header[1]:<9} {header[2]:>9} {header[3]:>30} {header[4]:>9}  verdict"]
    for result in results:
        skill = result.get("skill") or {}
        hit_rate = result.get("hit_rate") or {}
        interval = f"{_format(skill.get('point'))} ({_format(skill.get('low'))} to {_format(skill.get('high'))})"
        hit = "n/a" if hit_rate.get("point") is None else f"{hit_rate['point']:.1%}"
        outcome = result["verdict"]
        label = "PASS" if outcome["passed"] else f"FAIL ({', '.join(outcome['reasons'])})"
        lines.append(
            f"{result['scenario']:<16} {result['expect']:<9} {result['n_forecasts']:>9} "
            f"{interval:>30} {hit:>9}  {label}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synthetic_truth.py",
        description="Check that the walk-forward harness reports an answer known in advance.",
    )
    parser.add_argument("--scenario", choices=[*sorted(SCENARIOS), "all"], default="all")
    parser.add_argument("--work-dir", type=Path, required=True, help="where archives, runs and the result are written")
    parser.add_argument("--tickers", type=int, default=8, help="simulated tickers per scenario")
    parser.add_argument("--rows", type=int, default=500, help="trading rows per ticker")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--strength", type=float, default=DEFAULT_STRENGTH, help="lag-1 correlation of the planted drift"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenarios = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = [
        run_scenario(
            scenario,
            work_dir=args.work_dir,
            n_tickers=args.tickers,
            n_rows=args.rows,
            seed=args.seed,
            strength=args.strength,
        )
        for scenario in scenarios
    ]
    print(render_results(results))
    result_path = Path(args.work_dir) / RESULT_FILENAME
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"result written to {result_path}")
    return 0 if all(result["verdict"]["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
