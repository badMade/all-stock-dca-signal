"""Scoring and report generation for wondrous-octopus walk-forward runs.

Two layers:

- :func:`score_forecast` runs ONCE per matured forecast, at reveal time in
  the harness, and produces the compact per-record scoring payload that gets
  ledgered (absolute/squared/pinball errors, in-band flags for both raw and
  calibrated bands, in-realized-range flags, band widths). Metrics are then
  a pure aggregation of ledgered scores -- the report never re-predicts.

- :class:`MetricsAccumulator` / :func:`build_report` stream score records
  into the regime x model x component tables of ``report.json``, and
  :func:`render_markdown` turns that into the human ``report.md`` (the same
  json+md pairing ml-forecaster's eval reports use).

Comparability: when several models are reported together, aggregation is
restricted to (ticker, origin_date) pairs present for ALL of them, via a
two-pass stream (pass 1 intersects keys, pass 2 aggregates). The harness
predicts every active model on the same worklist, so in practice the
intersection only bites when a model joined a run late. The intersection
keys are held in memory as sorted numpy int64 arrays, not Python sets: at
full-universe/26-year scale a model can carry ~39M scored pairs, and a
boxed Python int in a set costs roughly 10x a raw int64 array element --
several GB of avoidable peak memory resident right when write_report()
runs, alongside the panel/models the replay just finished with (Codex
review: "Stream report intersections without Python sets").
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping

import numpy as np
from data import COMPONENTS
from model_interface import QUANTILE_KEYS, QUANTILES

#: Named market-regime windows, bucketed by target_date. Chosen to cover the
#: canonical stress periods a 2000->present replay walks through; everything
#: else lands in "other", and "all" spans the whole run.
REGIME_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("dotcom_bust", "2000-01-01", "2003-03-31"),
    ("gfc", "2007-10-01", "2009-06-30"),
    ("covid", "2020-02-01", "2020-12-31"),
    ("rate_shock_2022", "2022-01-01", "2022-12-31"),
)

#: The coverage the p10-p90 band nominally promises.
NOMINAL_COVERAGE = 0.8


def bucket_regime(target_date: str) -> str:
    for name, start, end in REGIME_WINDOWS:
        if start <= target_date <= end:
            return name
    return "other"


def _pinball(quantile: float, predicted: float, actual: float) -> float:
    error = actual - predicted
    return max(quantile * error, (quantile - 1.0) * error)


def _bounded_log_return(value: float) -> float:
    # Guards every model-derived log-return value entering score_forecast()
    # (point AND quantile values): an unbounded model head (Octopus) could
    # in principle emit a non-finite or extreme log-return, and this
    # function's OWN abs_err/sq_err/pinball/in_band computations would
    # otherwise poison MetricsAccumulator's running per-bucket sums with
    # NaN forever (a single NaN addend poisons a running sum permanently),
    # not just the price_abs_err conversion the first pass here guarded
    # (Codex review: "Reject non-finite forecasts before scoring", a
    # follow-up on "Make score conversion safe for unbounded model
    # outputs" -- that fix only reached the exponentiated price
    # conversion, not these primary log-space metrics).
    if math.isnan(value):
        # A NaN model output is not "predicted zero return" -- it's an
        # invalid forecast. Silently substituting 0.0 let a broken model's
        # NaN register as an accurate flat forecast (and even earn in-band
        # credit against a realized move near zero) instead of being
        # scored as the miss it actually is, letting a numerically failed
        # model appear competitive across every accuracy/skill/calibration
        # report (Codex review: "Reject NaN forecasts instead of scoring
        # them as zero"). Clip to the SAME extreme-value sentinel the
        # finite branch below already uses for a genuinely unbounded
        # value: real market log-returns never approach +/-50, so this
        # always registers as a large error and an out-of-band miss
        # rather than a plausible prediction.
        return 50.0
    return max(-50.0, min(50.0, value))


def _bounded_exp(log_return: float) -> float:
    # math.exp() raises OverflowError past roughly +/-709 on an unbounded
    # value; this guards the SCORING path itself -- a crash here happens
    # inside the harness's normal reveal/step() flow, and since the
    # model's checkpointed state (and therefore its next prediction) is
    # deterministic, the same crash would recur on every resume,
    # permanently stalling the run instead of just corrupting one display.
    return math.exp(_bounded_log_return(log_return))


def score_forecast(
    *,
    point: dict[str, float],
    quantiles: dict[str, dict[str, float]],
    quantiles_raw: dict[str, dict[str, float]],
    actual: dict[str, float],
    ref_close: float,
) -> dict[str, Any]:
    """The per-record scoring payload ledgered at reveal time.

    ``quantiles`` are the calibrated bands actually presented to the user;
    ``quantiles_raw`` are the model's own pre-calibration bands -- both are
    scored so the report can show calibration's contribution honestly.
    All errors live in log-return space except ``price_abs_err``, which
    converts through ``ref_close`` into adjusted dollars.
    """
    # Sanitized ONCE, up front, and used everywhere below (including the
    # close_in_range/pred_close fields) rather than per-computation: an
    # unbounded model head's non-finite/extreme output must not leak
    # through ANY field of this record, since MetricsAccumulator sums
    # several of them (abs_err, sq_err, pred_close, ...) into running
    # per-bucket/per-day totals that a single NaN poisons permanently.
    has_non_finite = any(
        not math.isfinite(val) for val in point.values()
    ) or any(
        not math.isfinite(val)
        for comp in quantiles.values()
        for val in comp.values()
    )
    point = {component: _bounded_log_return(point[component]) for component in COMPONENTS}
    quantiles = {
        component: {key: _bounded_log_return(quantiles[component][key]) for key in QUANTILE_KEYS}
        for component in COMPONENTS
    }
    quantiles_raw = {
        component: {key: _bounded_log_return(quantiles_raw[component][key]) for key in QUANTILE_KEYS}
        for component in COMPONENTS
    }
    abs_err: dict[str, float] = {}
    sq_err: dict[str, float] = {}
    price_abs_err: dict[str, float] = {}
    pinball: dict[str, dict[str, float]] = {}
    in_band: dict[str, bool] = {}
    in_band_raw: dict[str, bool] = {}
    band_width: dict[str, float] = {}
    for component in COMPONENTS:
        predicted = point[component]
        realized = actual[component]
        error = predicted - realized
        abs_err[component] = abs(error)
        sq_err[component] = error * error
        price_abs_err[component] = abs(_bounded_exp(predicted) - _bounded_exp(realized)) * ref_close
        pinball[component] = {
            key: _pinball(quantile, quantiles[component][key], realized)
            for key, quantile in zip(QUANTILE_KEYS, QUANTILES, strict=True)
        }
        in_band[component] = quantiles[component]["p10"] <= realized <= quantiles[component]["p90"]
        in_band_raw[component] = quantiles_raw[component]["p10"] <= realized <= quantiles_raw[component]["p90"]
        band_width[component] = quantiles[component]["p90"] - quantiles[component]["p10"]
    return {
        "abs_err": abs_err,
        "sq_err": sq_err,
        "price_abs_err": price_abs_err,
        "pinball": pinball,
        "in_band": in_band,
        "in_band_raw": in_band_raw,
        "band_width": band_width,
        "non_finite": has_non_finite,
        # "Inside the realized values": did the point land within the day's
        # actual traded range? Scored for close and open, the two components
        # a user most naturally reads as "the price tomorrow".
        "close_in_range": actual["low"] <= point["close"] <= actual["high"],
        "open_in_range": actual["low"] <= point["open"] <= actual["high"],
        "pred_close": point["close"],
        "actual_close": actual["close"],
    }


class _Welford:
    """Streaming mean/variance accumulator (no sample storage)."""

    __slots__ = ("count", "mean", "m2")

    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)

    @property
    def variance(self) -> float:
        return self.m2 / self.count if self.count else 0.0


class MetricsAccumulator:
    """Streams score records into per (model, regime, component) aggregates."""

    def __init__(self) -> None:
        self._sums: dict[tuple, dict[str, float]] = defaultdict(
            lambda: {
                "n": 0.0,
                "abs_err": 0.0,
                "sq_err": 0.0,
                "price_abs_err": 0.0,
                "pinball_p10": 0.0,
                "pinball_p50": 0.0,
                "pinball_p90": 0.0,
                "in_band": 0.0,
                "in_band_raw": 0.0,
                "band_width": 0.0,
            }
        )
        self._range_hits: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"n": 0.0, "close_in_range": 0.0, "open_in_range": 0.0}
        )
        # Per (model, target_date): sums for the market-entity view.
        self._daily: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"n": 0.0, "pred_close": 0.0, "actual_close": 0.0, "err_close": 0.0}
        )
        self._close_err_all: dict[str, _Welford] = defaultdict(_Welford)

    def add_score(self, record: Mapping[str, Any]) -> None:
        model = record["model"]
        regime = bucket_regime(record["target_date"])
        score = record["score"]
        for bucket in (regime, "all"):
            for component in COMPONENTS:
                sums = self._sums[(model, bucket, component)]
                sums["n"] += 1
                sums["abs_err"] += score["abs_err"][component]
                sums["sq_err"] += score["sq_err"][component]
                sums["price_abs_err"] += score["price_abs_err"][component]
                for key in QUANTILE_KEYS:
                    sums[f"pinball_{key}"] += score["pinball"][component][key]
                sums["in_band"] += 1.0 if score["in_band"][component] else 0.0
                sums["in_band_raw"] += 1.0 if score["in_band_raw"][component] else 0.0
                sums["band_width"] += score["band_width"][component]
            hits = self._range_hits[(model, bucket)]
            hits["n"] += 1
            hits["close_in_range"] += 1.0 if score["close_in_range"] else 0.0
            hits["open_in_range"] += 1.0 if score["open_in_range"] else 0.0
        daily = self._daily[(model, record["target_date"])]
        close_error = score["pred_close"] - score["actual_close"]
        daily["n"] += 1
        daily["pred_close"] += score["pred_close"]
        daily["actual_close"] += score["actual_close"]
        daily["err_close"] += close_error
        self._close_err_all[model].add(close_error)

    def report(self) -> dict[str, Any]:
        models = sorted({model for model, _, _ in self._sums})
        regimes = [name for name, _, _ in REGIME_WINDOWS] + ["other", "all"]
        by_regime: dict[str, Any] = {}
        for regime in regimes:
            regime_block: dict[str, Any] = {}
            for model in models:
                model_block: dict[str, Any] = {}
                for component in COMPONENTS:
                    sums = self._sums.get((model, regime, component))
                    if not sums or sums["n"] == 0:
                        continue
                    n = sums["n"]
                    model_block[component] = {
                        "n": int(n),
                        "mae": sums["abs_err"] / n,
                        "rmse": math.sqrt(sums["sq_err"] / n),
                        "mae_price": sums["price_abs_err"] / n,
                        "pinball": {key: sums[f"pinball_{key}"] / n for key in QUANTILE_KEYS},
                        "coverage": sums["in_band"] / n,
                        "coverage_raw": sums["in_band_raw"] / n,
                        "band_width": sums["band_width"] / n,
                    }
                if model_block:
                    regime_block[model] = model_block
            if regime_block:
                by_regime[regime] = regime_block
        range_hit: dict[str, Any] = {}
        for model in models:
            hits = self._range_hits.get((model, "all"))
            if hits and hits["n"]:
                range_hit[model] = {
                    "close_in_range": hits["close_in_range"] / hits["n"],
                    "open_in_range": hits["open_in_range"] / hits["n"],
                    "n": int(hits["n"]),
                }
        market: dict[str, Any] = {}
        for model in models:
            days = [key for key in self._daily if key[0] == model]
            if not days:
                continue
            aggregate_errors = []
            overall = self._close_err_all[model]
            between_sum = 0.0
            weight_total = 0.0
            for key in days:
                daily = self._daily[key]
                if daily["n"] == 0:
                    continue
                mean_pred = daily["pred_close"] / daily["n"]
                mean_actual = daily["actual_close"] / daily["n"]
                aggregate_errors.append(abs(mean_pred - mean_actual))
                day_mean_error = daily["err_close"] / daily["n"]
                # Weight each day's mean by that day's forecast count: the
                # law-of-total-variance decomposition (between-day over
                # total) is a true fraction in [0, 1] only under consistent
                # weights -- an unweighted day-mean variance can exceed the
                # per-forecast total when daily counts vary (Codex review:
                # "Compute the market-wide variance fraction with
                # consistent weights").
                between_sum += daily["n"] * (day_mean_error - overall.mean) ** 2
                weight_total += daily["n"]
            total_variance = overall.variance
            between_variance = between_sum / weight_total if weight_total else 0.0
            market[model] = {
                "aggregate_close_mae": sum(aggregate_errors) / len(aggregate_errors) if aggregate_errors else 0.0,
                "n_days": len(aggregate_errors),
                # What fraction of close-error variance is market-wide (the
                # day's shared error) rather than idiosyncratic? 1.0 = every
                # mistake is a market-level mistake; ~0 = mistakes are all
                # ticker-specific.
                "error_variance_market_fraction": (
                    between_variance / total_variance if total_variance > 0 else 0.0
                ),
            }
        skill: dict[str, Any] = {}
        persistence_key = "persistence"
        if persistence_key in models:
            for model in models:
                if model == persistence_key:
                    continue
                model_skill = {}
                for component in COMPONENTS:
                    ours = self._sums.get((model, "all", component))
                    theirs = self._sums.get((persistence_key, "all", component))
                    if ours and theirs and ours["n"] and theirs["n"]:
                        our_mae = ours["abs_err"] / ours["n"]
                        their_mae = theirs["abs_err"] / theirs["n"]
                        if their_mae > 0:
                            model_skill[component] = 1.0 - our_mae / their_mae
                if model_skill:
                    skill[model] = model_skill
        return {
            "models": models,
            "nominal_coverage": NOMINAL_COVERAGE,
            "by_regime": by_regime,
            "range_hit": range_hit,
            "market": market,
            "skill_vs_persistence": skill,
        }


def build_report(
    records_by_model: Mapping[str, Callable[[], Iterable[Mapping[str, Any]]]],
    *,
    intersect: bool = True,
    universe: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate each model's score-record stream into the report dict.

    ``records_by_model`` maps model name to a CALLABLE returning a fresh
    record iterator -- callables because strict intersection needs two
    passes (see module docstring). With ``intersect=False`` (single-model
    runs) one pass suffices.
    """
    # Compact integer encoding instead of a set of (ticker, origin_date)
    # string-tuple objects: a full-universe replay's intersection otherwise
    # materializes tens of millions of Python tuples simultaneously across
    # models -- many gigabytes just for bookkeeping (Codex review: "Avoid
    # materializing every scored key for report intersection"). The
    # ticker/date vocabularies are tiny (bounded by the universe size and
    # calendar length, not by record count), so these lookup dicts stay
    # small even when the intersection itself covers the whole replay; a
    # plain int in a set is also far cheaper than a 2-tuple of strings.
    ticker_ids: dict[str, int] = {}
    date_ids: dict[str, int] = {}
    # Safely larger than any realistic calendar length (even centuries of
    # daily trading days) so ticker_id/date_id pairs never collide.
    _DATE_ID_SPACE = 10_000_000

    def _key_id(ticker: str, origin_date: str) -> int:
        ticker_id = ticker_ids.setdefault(ticker, len(ticker_ids))
        date_id = date_ids.setdefault(origin_date, len(date_ids))
        return ticker_id * _DATE_ID_SPACE + date_id

    # Sorted numpy int64 array, not a Python set: at full-universe scale a
    # single model's key set (~39M pairs) costs several GB as boxed Python
    # ints in a set's hash table, resident right when this report runs
    # (Codex review: "Stream report intersections without Python sets").
    # np.intersect1d on already-unique, already-sorted arrays
    # (assume_unique=True) is a linear merge, no extra full-size
    # allocation beyond the result itself.
    keep: np.ndarray | None = None
    if intersect and len(records_by_model) > 1:
        for _model, stream in records_by_model.items():
            keys = np.fromiter(
                (_key_id(record["ticker"], record["origin_date"]) for record in stream()), dtype=np.int64
            )
            keys = np.unique(keys)  # sorts and deduplicates in one pass
            keep = keys if keep is None else np.intersect1d(keep, keys, assume_unique=True)
    accumulator = MetricsAccumulator()
    counts: dict[str, int] = {}
    for model, stream in records_by_model.items():
        count = 0
        for record in stream():
            if keep is not None:
                key = _key_id(record["ticker"], record["origin_date"])
                position = np.searchsorted(keep, key)
                if position >= len(keep) or keep[position] != key:
                    continue
            accumulator.add_score(record)
            count += 1
        counts[model] = count
    report = accumulator.report()
    report["n_scored"] = counts
    report["intersection_applied"] = keep is not None
    if universe:
        report["universe"] = universe
    if diagnostics:
        report["diagnostics"] = diagnostics
    return report


def latest_retrain_date(records: Iterable[dict[str, Any]]) -> str | None:
    """Latest ``origin_date`` among a model's ledgered retrain markers, or ``None``."""
    dates = [record["origin_date"] for record in records]
    return max(dates) if dates else None


def diagnostic_health(
    records: Iterable[dict[str, Any]],
    *,
    window_days: int = 20,
    degrade_factor: float = 2.0,
    retrained_after: str | None = None,
) -> dict:
    """Encoder-health verdict from a model's diagnostic record stream.

    Per masking mode: the mean close-reconstruction error over the FIRST
    ``window_days`` distinct dates is the baseline (how good the encoder was
    right after its fit); the mean over the LAST ``window_days`` distinct
    dates is the current level. Current worse than ``degrade_factor`` x
    baseline => DEGRADED. Telemetry only -- the daily digest surfaces it and
    suggests a recovery retrain; nothing acts on it automatically.

    ``retrained_after``, when given, drops every record dated on or before
    that date first -- otherwise a recovery retrain's freshly-refit encoder
    would forever be compared against the ORIGINAL run's pre-recovery
    baseline (or hide its own regressions behind that stale, unrelated
    number) instead of establishing its own (Codex review: "Reset encoder-
    health baseline after recovery retraining").

    Aggregates each date to a running (sum, count) while streaming, not a
    list of every individual record: only the per-date MEAN ever matters
    (baseline/current below), and on the documented full replay
    (diagnostic_sample=500, two Octopus masking modes, multi-decade
    history) a list-of-every-value accumulator held on the order of
    millions of Python floats in memory for a computation that only ever
    reads the first/last window_days dates' worth of it (Codex review:
    "Bound encoder-health aggregation to the needed windows").
    """
    by_mode: dict[str, dict[str, list[float | int]]] = {}
    for record in records:
        if retrained_after is not None and record["origin_date"] <= retrained_after:
            continue
        mode = record.get("mode", "time")
        per_date = by_mode.setdefault(mode, {})
        # [sum, count], not a growing list of every abs_err -- see the
        # docstring: only the per-date mean is ever read below.
        entry = per_date.setdefault(record["origin_date"], [0.0, 0])
        entry[0] += record["abs_err"]["close"]
        entry[1] += 1
    verdict: dict[str, Any] = {"status": "OK", "modes": {}}
    for mode, per_date in by_mode.items():
        dates = sorted(per_date)
        # The first/last windows must be fully DISJOINT for "current worse
        # than baseline" to mean anything: with fewer than 2*window_days
        # dates they share dates (or are identical below window_days),
        # which trivially reports OK regardless of how badly reconstruction
        # has actually degraded since day one -- and this status feeds the
        # daily recovery-retrain suggestion (Codex review: "Withhold
        # encoder health until windows are independent").
        if len(dates) < 2 * window_days:
            continue
        first = dates[:window_days]
        last = dates[-window_days:]
        baseline = sum(per_date[date][0] / per_date[date][1] for date in first) / len(first)
        current = sum(per_date[date][0] / per_date[date][1] for date in last) / len(last)
        degraded = baseline > 0 and current > degrade_factor * baseline
        verdict["modes"][mode] = {
            "baseline_mae_close": baseline,
            "current_mae_close": current,
            "degraded": degraded,
        }
        if degraded:
            verdict["status"] = "DEGRADED"
    if not verdict["modes"]:
        verdict["status"] = "INSUFFICIENT_DATA"
    return verdict


def _format_pct(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    """The human-facing report.md, mirroring ml-forecaster's eval_report tone."""
    lines: list[str] = ["# Wondrous Octopus — walk-forward report", ""]
    if "universe" in report:
        universe = report["universe"]
        lines.append(
            f"Universe: {universe.get('n_tickers', '?')} tickers "
            f"({universe.get('description', 'unlabeled subset')}); "
            f"scored records per model: {report.get('n_scored', {})}."
        )
        lines.append("")
    lines.append(
        f"Band target: p10–p90 nominal coverage {_format_pct(report['nominal_coverage'])}. "
        "`coverage` is the calibrated band; `coverage_raw` is the model's own band."
    )
    lines.append("")
    for regime, regime_block in report.get("by_regime", {}).items():
        lines.append(f"## Regime: {regime}")
        lines.append("")
        lines.append("| model | component | n | MAE (log) | RMSE | MAE ($) | coverage | raw | band width |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for model, model_block in regime_block.items():
            for component, stats in model_block.items():
                lines.append(
                    f"| {model} | {component} | {stats['n']} | {stats['mae']:.5f} | "
                    f"{stats['rmse']:.5f} | {stats['mae_price']:.4f} | "
                    f"{_format_pct(stats['coverage'])} | {_format_pct(stats['coverage_raw'])} | "
                    f"{stats['band_width']:.5f} |"
                )
        lines.append("")
    if report.get("range_hit"):
        lines.append("## Point forecasts inside the realized range")
        lines.append("")
        lines.append("| model | close in [low, high] | open in [low, high] | n |")
        lines.append("|---|---|---|---|")
        for model, hits in report["range_hit"].items():
            lines.append(
                f"| {model} | {_format_pct(hits['close_in_range'])} | "
                f"{_format_pct(hits['open_in_range'])} | {hits['n']} |"
            )
        lines.append("")
    if report.get("skill_vs_persistence"):
        lines.append("## Skill vs persistence (1 − MAE/MAE_persistence; higher is better)")
        lines.append("")
        lines.append("| model | open | high | low | close |")
        lines.append("|---|---|---|---|---|")
        for model, model_skill in report["skill_vs_persistence"].items():
            cells = " | ".join(_format_pct(model_skill.get(component, float("nan"))) for component in COMPONENTS)
            lines.append(f"| {model} | {cells} |")
        lines.append("")
    if report.get("market"):
        lines.append("## The market as one entity")
        lines.append("")
        lines.append("| model | aggregate close MAE | market-wide error fraction | days |")
        lines.append("|---|---|---|---|")
        for model, stats in report["market"].items():
            lines.append(
                f"| {model} | {stats['aggregate_close_mae']:.5f} | "
                f"{_format_pct(stats['error_variance_market_fraction'])} | {stats['n_days']} |"
            )
        lines.append("")
    return "\n".join(lines)
