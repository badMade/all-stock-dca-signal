#!/usr/bin/env python3
"""The trust scorecard: how well the p10-p90 bands are doing, judged over time.

    python wondrous-octopus/trust_scorecard.py --run-dir <run>
    python wondrous-octopus/trust_scorecard.py --run-dir <run> --as-of 2026-11-13 --dry-run

One row per (as-of date, model, horizon), appended to
``<run>/trust/trust_scorecard.jsonl`` and never rewritten. Each row is a
rolling-window reading of the band -- coverage, breach side, interval score,
skill against persistence -- with a date-block bootstrap interval around it
(see ``band_uncertainty``), and a status:

- ``ok``       the coverage interval contains the nominal target;
- ``flagged``  the interval excludes it -- too narrow OR too wide;
- ``unknown``  not enough history to say.

``unknown`` is a status of its own on purpose: a scorecard that reads "ok"
for a 90-day horizon on six weeks of data would be claiming evidence that
does not exist. The flag is a label on research output and nothing more. It
reaches no order, no sizing and no other component; Octopus stays
suggest-only (see ``docs/wondrous_octopus.md``).

Why a rolling window and an append-only file rather than one cumulative
number: cumulative coverage over 26 years cannot move, so a model that
degraded last quarter hides behind twenty good years. And a history that
could be recomputed could be rationalized -- each row records what was
knowable on its as-of date and stays that way.

Reads the ledger only through the checkpoint-bounded reader the other
inspection tools use, takes no run lock, and writes only under ``trust/``.
Torch-free.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argparse  # noqa: E402
import bisect  # noqa: E402
import json  # noqa: E402
from collections import defaultdict  # noqa: E402
from typing import Any, Iterable, Iterator, Mapping, Sequence  # noqa: E402

import numpy as np  # noqa: E402
from band_uncertainty import (  # noqa: E402
    DailyBandSeries,
    Interval,
    christoffersen_independence,
    daily_band_series,
    kupiec_pof,
    ratio_interval,
    skill_interval,
)
from ledger import LedgerError  # noqa: E402
from metrics import NOMINAL_COVERAGE  # noqa: E402

#: Horizons tracked by default (operator sign-off 2026-11-14): next day, one
#: week, one month, one quarter, and the full 90-row reach of the model.
DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 21, 63, 90)

#: Rolling window in trading dates (operator sign-off 2026-11-14): about one
#: quarter.
DEFAULT_WINDOW = 63

#: Shortest bootstrap block. Even at horizon 1, daily volatility clusters,
#: so single-day resampling would understate the interval.
MIN_BLOCK_LENGTH = 5

#: Blocks a full window should hold. Two is the minimum from which an
#: interval can be estimated at all (``band_uncertainty.MIN_BLOCKS``); four
#: leaves some room before a single unusual block decides the answer.
WINDOW_BLOCKS = 4

#: The model every other model's skill is measured against.
BASELINE_MODEL = "persistence"

#: Per-ticker coverage tests need a sequence long enough to have breaches in
#: it at all: at a 20% miss rate, 30 days expects six.
MIN_TEST_OBSERVATIONS = 30

#: Significance level of the per-ticker coverage tests.
TEST_ALPHA = 0.05

HISTORY_RELPATH = Path("trust") / "trust_scorecard.jsonl"


class ScorecardError(RuntimeError):
    """The history file cannot be trusted as it stands; nothing was written."""


def block_length(horizon: int) -> int:
    """Bootstrap block for a horizon: at least the horizon itself.

    Horizon-h targets from consecutive origin days share h-1 days of realized
    returns; a block shorter than h would cut one dependent run in two and
    treat the halves as independent.
    """
    return max(int(horizon), MIN_BLOCK_LENGTH)


def window_length(horizon: int, window: int = DEFAULT_WINDOW) -> int:
    """Rolling window for a horizon: the signed-off window, or four blocks.

    At horizon 63 the signed-off 63 dates are a single block, and at horizon
    90 less than one -- no interval can come from that. Long horizons widen
    to ``WINDOW_BLOCKS`` blocks; short ones keep the 63 dates as agreed.
    """
    return max(int(window), WINDOW_BLOCKS * block_length(horizon))


def _interval_payload(interval: Interval) -> dict[str, Any]:
    return {"point": interval.point, "low": interval.low, "high": interval.high, "reason": interval.reason}


def _judge(coverage: Interval, target: float) -> tuple[str, list[str]]:
    """Status and reasons from the coverage interval alone.

    Coverage is the one quantity with a promised value to hold it to; the
    interval score and skill have no absolute target, so they are reported
    for comparison over time and never decide the status.
    """
    if coverage.low is None or coverage.high is None:
        return "unknown", [coverage.reason or "interval_unavailable"]
    if coverage.high < target:
        return "flagged", ["coverage_below_target"]
    if coverage.low > target:
        return "flagged", ["coverage_above_target"]
    return "ok", []


def _window_slice(dates: Sequence[str], as_of: str, length: int) -> slice:
    # bisect_right on ISO dates: every date on or before as_of, and nothing
    # after it. This line is the scorecard's whole lookahead guard.
    end = bisect.bisect_right(dates, as_of)
    return slice(max(0, end - length), end)


def scorecard_row(
    series: DailyBandSeries | None,
    *,
    model: str,
    horizon: int,
    as_of: str,
    baseline: DailyBandSeries | None = None,
    window: int = DEFAULT_WINDOW,
    target: float = NOMINAL_COVERAGE,
    level: float = 0.95,
    seed: int = 0,
) -> dict[str, Any]:
    """One row: the band's rolling-window reading as knowable on ``as_of``."""
    block = block_length(horizon)
    length = window_length(horizon, window)
    row: dict[str, Any] = {
        "as_of": as_of,
        "model": model,
        "horizon": int(horizon),
        "window_dates": 0,
        "window_start": None,
        "window_end": None,
        "block_length": block,
        "n": 0,
        "target_coverage": target,
        "coverage": _interval_payload(Interval(None, None, None, 0, block, "no_observations")),
        "below_rate": None,
        "above_rate": None,
        "interval_score": _interval_payload(Interval(None, None, None, 0, block, "no_observations")),
        "skill_vs_persistence": None,
        "coverage_tests": None,
        "status": "unknown",
        "reasons": ["no_observations"],
    }
    if series is None:
        return row
    chosen = _window_slice(series.dates, as_of, length)
    dates = series.dates[chosen]
    if not dates:
        return row
    n = series.n[chosen]
    coverage = ratio_interval(series.in_band[chosen], n, block_length=block, level=level, seed=seed)
    sided = float(series.n_sided[chosen].sum())
    row.update(
        {
            "window_dates": len(dates),
            "window_start": dates[0],
            "window_end": dates[-1],
            "n": int(n.sum()),
            "coverage": _interval_payload(coverage),
            # Over n_sided, not n: records from before the band scorecard
            # cannot say which side a miss fell on (see DailyBandSeries).
            "below_rate": float(series.below[chosen].sum()) / sided if sided else None,
            "above_rate": float(series.above[chosen].sum()) / sided if sided else None,
            "interval_score": _interval_payload(
                ratio_interval(series.interval_score[chosen], n, block_length=block, level=level, seed=seed)
            ),
        }
    )
    if baseline is not None:
        row["skill_vs_persistence"] = _skill_payload(series, baseline, dates, block=block, level=level, seed=seed)
    row["status"], row["reasons"] = _judge(coverage, target)
    return row


def _skill_payload(
    series: DailyBandSeries, baseline: DailyBandSeries, dates: Sequence[str], *, block: int, level: float, seed: int
) -> dict[str, Any]:
    """Skill on interval score, over the window dates BOTH models were scored on.

    A date only one of them has is dropped: comparing the model's full
    window with a shorter baseline window would compare two different sets
    of days and call the difference skill. Per-date MEAN scores are
    compared, so a date on which the two scored different ticker counts
    still contributes like-for-like.
    """
    model_at = {date: index for index, date in enumerate(series.dates)}
    baseline_at = {date: index for index, date in enumerate(baseline.dates)}
    common = [date for date in dates if date in baseline_at]
    model_rows = [model_at[date] for date in common]
    baseline_rows = [baseline_at[date] for date in common]
    with np.errstate(invalid="ignore", divide="ignore"):
        model_loss = np.nan_to_num(series.interval_score[model_rows] / series.n[model_rows])
        baseline_loss = np.nan_to_num(baseline.interval_score[baseline_rows] / baseline.n[baseline_rows])
    return _interval_payload(skill_interval(model_loss, baseline_loss, block_length=block, level=level, seed=seed))


def coverage_test_shares(
    breaches_by_ticker: Mapping[str, Sequence[bool]],
    *,
    expected_rate: float = 1.0 - NOMINAL_COVERAGE,
    alpha: float = TEST_ALPHA,
    min_observations: int = MIN_TEST_OBSERVATIONS,
) -> dict[str, Any]:
    """Share of tickers whose own breach sequence fails each coverage test.

    Kupiec and Christoffersen are defined on ONE time-ordered sequence, so
    they are run per ticker and summarized. A sound band still rejects for
    about ``alpha`` of tickers by chance; a share far above that is the
    signal. Tickers are not independent of each other, so the share is
    reported as context and does not decide the row's status.
    """
    tested = 0
    rate_rejections = 0
    independence_tested = 0
    independence_rejections = 0
    for sequence in breaches_by_ticker.values():
        if len(sequence) < min_observations:
            continue
        tested += 1
        rate = kupiec_pof(
            n_observations=len(sequence),
            n_breaches=sum(1 for breach in sequence if breach),
            expected_rate=expected_rate,
        )
        if rate.p_value is not None and rate.p_value < alpha:
            rate_rejections += 1
        independence = christoffersen_independence(sequence)
        # Undefined for a ticker with no breach (or nothing but breaches) in
        # the window: left out of the denominator rather than counted as a
        # pass it never earned.
        if independence.p_value is not None:
            independence_tested += 1
            if independence.p_value < alpha:
                independence_rejections += 1
    return {
        "tickers_tested": tested,
        "kupiec_reject_share": (rate_rejections / tested) if tested else None,
        "independence_reject_share": (independence_rejections / independence_tested) if independence_tested else None,
    }


def _tee_ticker_breaches(
    records: Iterable[Mapping[str, Any]],
    as_of: str,
    horizons: frozenset[int],
    sink: dict[str, dict[str, list[tuple[str, bool]]]],
) -> Iterator[Mapping[str, Any]]:
    """Filter records to the scorecard's scope and, in the same pass, collect
    each ticker's horizon-1 close breaches.

    One pass on purpose: a full-universe ledger holds tens of millions of
    score records, and the series and the per-ticker sequences both need
    every one of them.
    """
    for record in records:
        if record["target_date"] > as_of:
            continue
        horizon = int(record.get("horizon", 1))
        if horizon not in horizons:
            continue
        if horizon == 1:
            breach = not record["score"]["in_band"]["close"]
            sink[record["model"]][record["ticker"]].append((record["target_date"], breach))
        yield record


def build_rows(
    records: Iterable[Mapping[str, Any]],
    *,
    as_of: str,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    window: int = DEFAULT_WINDOW,
    target: float = NOMINAL_COVERAGE,
    level: float = 0.95,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Rows for every model seen and every requested horizon, as of ``as_of``.

    A requested horizon with no data still gets a row (status ``unknown``):
    a missing row would read as "nothing to report" rather than "nothing is
    known".
    """
    wanted = frozenset(int(horizon) for horizon in horizons)
    ticker_breaches: dict[str, dict[str, list[tuple[str, bool]]]] = defaultdict(lambda: defaultdict(list))
    series = daily_band_series(_tee_ticker_breaches(records, as_of, wanted, ticker_breaches))
    models = sorted({model for model, _ in series})
    rows: list[dict[str, Any]] = []
    for model in models:
        for horizon in sorted(wanted):
            baseline = series.get((BASELINE_MODEL, horizon)) if model != BASELINE_MODEL else None
            row = scorecard_row(
                series.get((model, horizon)),
                model=model,
                horizon=horizon,
                as_of=as_of,
                baseline=baseline,
                window=window,
                target=target,
                level=level,
                seed=seed,
            )
            if horizon == 1 and row["window_start"] is not None:
                row["coverage_tests"] = coverage_test_shares(
                    _windowed_sequences(ticker_breaches[model], row["window_start"]), expected_rate=1.0 - target
                )
            rows.append(row)
    return rows


def _windowed_sequences(
    by_ticker: Mapping[str, list[tuple[str, bool]]], window_start: str
) -> dict[str, list[bool]]:
    # Sorted by date before testing: ledger shards are keyed by ORIGIN year
    # and a recovered forecast can mature out of order, and the independence
    # test is about which breach followed which.
    sequences: dict[str, list[bool]] = {}
    for ticker, observations in by_ticker.items():
        sequences[ticker] = [breach for date, breach in sorted(observations) if date >= window_start]
    return sequences


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, int]:
    return (str(row["as_of"]), str(row["model"]), int(row["horizon"]))


def load_history(path: Path) -> list[dict[str, Any]]:
    """Every recorded row, oldest first. A missing file is an empty history."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ScorecardError(f"REFUSED: {path} line {number} is not valid JSON ({error.msg})") from error
        if not isinstance(row, dict) or not {"as_of", "model", "horizon"} <= set(row):
            raise ScorecardError(f"REFUSED: {path} line {number} is not a scorecard row")
        rows.append(row)
    return rows


def append_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> int:
    """Append the rows not already recorded; return how many were added.

    A row whose (as_of, model, horizon) is already present is skipped, never
    replaced: a retried job must not double-count a day, and a later run
    that would judge the same day differently must not edit what was
    recorded at the time. An unreadable history is refused rather than
    appended to -- new rows after a damaged line would bury the damage.

    Written through a same-directory temp file and an atomic rename (the
    ``daily_update._atomic_write_text`` pattern), with the existing bytes
    copied through unchanged, so a reader never sees a half-written line.
    """
    existing = {_row_key(row) for row in load_history(path)}
    fresh: list[Mapping[str, Any]] = []
    for row in rows:
        key = _row_key(row)
        if key not in existing:
            existing.add(key)
            fresh.append(row)
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_bytes() if path.exists() else b""
    if before and not before.endswith(b"\n"):
        before += b"\n"
    addition = "".join(json.dumps(row, sort_keys=True) + "\n" for row in fresh).encode("utf-8")
    tmp_path = path.parent / f"{path.name}.tmp"
    tmp_path.write_bytes(before + addition)
    tmp_path.replace(path)
    return len(fresh)


def _format_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    """A fixed-width table for the terminal and scheduled-job logs."""
    lines = [
        f"{'model':<14} {'h':>3} {'dates':>5} {'coverage':>9} {'95% interval':>17} {'below':>6} {'above':>6}  status"
    ]
    for row in rows:
        coverage = row["coverage"]
        interval = (
            f"{coverage['low']:.1%} to {coverage['high']:.1%}"
            if coverage["low"] is not None and coverage["high"] is not None
            else "unknown"
        )
        status = row["status"] + (f" ({', '.join(row['reasons'])})" if row["reasons"] else "")
        lines.append(
            f"{row['model']:<14} {row['horizon']:>3} {row['window_dates']:>5} {_format_rate(coverage['point']):>9} "
            f"{interval:>17} {_format_rate(row['below_rate']):>6} {_format_rate(row['above_rate']):>6}  {status}"
        )
    return "\n".join(lines)


#: How many origin years before the as-of year are read. The longest window
#: is 360 trading dates (about a year and a half) and the longest horizon 90
#: rows, so two years reach every forecast that can mature inside it; the
#: third is margin. Shards are keyed by ORIGIN year, so a ticker that
#: resumed trading after a gap longer than this has its one late-maturing
#: forecast left out -- accepted here, where reading all 26 years of a
#: full-universe ledger on every daily run is the alternative (the same
#: cost daily_update._day_scores() was changed to avoid).
ORIGIN_YEARS_BACK = 3


def _ledger_records(ledger: Any, *, min_year: str | None = None) -> Iterator[Mapping[str, Any]]:
    for model in ledger.models():
        for kind in ("score", "horizon_score"):
            for year in ledger.available_years(model=model, kind=kind):
                if min_year is None or year >= min_year:
                    yield from ledger.iter_records(model=model, kind=kind, year=year)


def _latest_target_date(ledger: Any) -> str | None:
    """The most recent scored date, read from each model's newest shards only."""
    latest: str | None = None
    for model in ledger.models():
        for kind in ("score", "horizon_score"):
            years = ledger.available_years(model=model, kind=kind)
            if not years:
                continue
            for record in ledger.iter_records(model=model, kind=kind, year=max(years)):
                if latest is None or record["target_date"] > latest:
                    latest = record["target_date"]
    return latest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trust_scorecard.py", description="Append the rolling band-trust scorecard for a Wondrous Octopus run."
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="a run directory holding a checkpoint")
    parser.add_argument("--as-of", default=None, help="score as knowable on this date (default: latest scored date)")
    parser.add_argument("--horizons", type=int, nargs="+", default=list(DEFAULT_HORIZONS))
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="rolling window in trading dates")
    parser.add_argument("--dry-run", action="store_true", help="print the rows; write nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Imported here, not at module top: inspect_run pulls in run_walkforward
    # and with it the model stack, which the pure functions above (and their
    # tests) do not need. Its _open_ledger() is the checkpoint-bounded reader
    # every inspection view must use -- an unrestricted Ledger() can show
    # records the next resume will delete.
    from inspect_run import Refused, _open_ledger

    run_dir = Path(args.run_dir)
    try:
        ledger = _open_ledger(run_dir)
        as_of = args.as_of or _latest_target_date(ledger)
        if as_of is None:
            raise Refused(f"no score records under {run_dir}; nothing to judge")
        min_year = str(int(as_of[:4]) - ORIGIN_YEARS_BACK)
        rows = build_rows(
            _ledger_records(ledger, min_year=min_year), as_of=as_of, horizons=args.horizons, window=args.window
        )
        print(f"Trust scorecard as of {as_of} ({run_dir})")
        print(render_rows(rows))
        if args.dry_run:
            print("dry run: nothing written")
            return 0
        path = run_dir / HISTORY_RELPATH
        added = append_rows(path, rows)
        print(f"{added} new row(s) appended to {path}")
    except (Refused, LedgerError, ScorecardError) as error:
        # Same convention as inspect_run.main(): a refusal is a clean,
        # prefixed message and a non-zero exit, never a traceback. A FLAGGED
        # band is not a refusal -- it is a result, printed above, exit 0.
        message = str(error)
        print(message if message.startswith("REFUSED") else f"REFUSED: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
