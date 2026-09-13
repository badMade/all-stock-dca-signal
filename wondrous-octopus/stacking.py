"""Regime-weighted ensemble over the forecasts three models already ledgered.

THE point of this module is that it needs nothing new. A ledgered ``score``
record carries the forecast (point + calibrated bands), the realized truth,
AND the per-quantile pinball loss, so an ensemble over the existing models
is a second pass over records that already exist on disk: no new training
pipeline, no re-running a model, no forecast/score join. That is what makes
this the cheapest real improvement available to this subproject.

THE invariant, inherited from features.py's no-lookahead rule: the weights
that blend a forecast maturing on day T are fitted ONLY on scores revealed
strictly before T. :func:`replay` enforces the ordering structurally --
weights are read once per target day, BEFORE any of that day's records are
blended, and that day's losses are folded in only afterwards. The
regression test in tests/test_stacking.py perturbs everything after a
cutoff and asserts nothing at or before it moves, exactly as
tests/test_features.py does for the feature layer.

Design choices worth keeping:

- **Inverse-loss weights, not a fitted meta-model.** Weights are an
  exponentially-weighted inverse mean-pinball per (model, component),
  non-negative by construction. A least-squares meta-weight could go
  negative and blow up out of sample; there is nothing to overfit here.
- **Vincentization** (averaging the quantile FUNCTIONS level by level, not
  the densities). One component's p10/p50/p90 all share that component's
  weights, and a convex combination of sorted vectors stays sorted, so the
  ensemble can never emit a CROSSED BAND.
  BAR validity is a different matter and is NOT guaranteed here. Convexity
  of max() would preserve `high >= max(open, close)` only under ONE weight
  vector shared by every component; weights are deliberately per-component,
  so open can follow model A while high follows model B. Two individually
  valid bars can then blend into an impossible one -- measured: models at
  (open 0.00, high 0.001) and (open 0.10, high 0.101), with open following
  the second and high the first, blend to high 0.001 below open 0.100.
  finalize_forecast() repairs that, so what leaves this module is always a
  possible bar, but the repair is real work and not defense in depth: its
  counts ride out on every record replay() emits, per this repo's
  repairs-are-counted-never-silent contract (Copilot review: "blend() can
  report nonzero repairs even when every input bar is valid").
- **A floor under every weight.** Today's worst model is the next regime's
  best, and a model at weight zero never generates the evidence that would
  win its weight back.
- **Decay once per revealed DAY, not per record.** A full-universe day
  matures thousands of scores; decaying per record would collapse a 60-day
  half-life into about a day of replay and make the weights depend on
  universe size. Same rule ConformalCalibrator.observe_batch() follows.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import numpy as np
from data import COMPONENTS
from harness import SCHEMA_VERSION
from ledger import Ledger
from metrics import score_forecast
from model_interface import QUANTILE_KEYS, RepairCounts, finalize_forecast

#: The synthetic model name the ensemble ledgers under. Deliberately just
#: another model name: metrics.py's regime x model x component tables and
#: every report that reads them pick it up with no changes at all.
ENSEMBLE_MODEL_NAME = "ensemble"

#: Half-life, in revealed days, of the exponentially-weighted loss. About a
#: quarter of trading: long enough not to chase a single bad week, short
#: enough to turn over inside a regime.
DEFAULT_HALF_LIFE_DAYS = 60.0

#: Minimum share of the blend every model keeps, however badly it is doing.
DEFAULT_FLOOR = 0.02

#: Guards 1/loss for a model that is briefly perfect on a quiet day.
_LOSS_EPS = 1e-9


def _distinct_models(models: Sequence[str], caller: str) -> tuple[str, ...]:
    """The model roster, refusing an empty or duplicated one.

    A duplicate name collapses in every per-model dict here but still counts
    toward len(models), and both consequences are SILENT:

    - weights() divides the roster's share among fewer keys than it counted,
      so ComponentWeights(("a", "a")) returned {"a": 0.5} -- a weight vector
      that does not sum to 1, breaking the invariant the blend relies on;
    - replay() requires len(entries) == len(models) to call a pair shared by
      every model, and a pooled entry can only ever have one key per distinct
      name, so ("a", "a") skipped every pair and yielded NOTHING rather than
      refusing.

    Fail closed instead: a caller naming the same model twice is confused
    about what it is asking for, and neither degraded answer is one it wants
    (Copilot review: "Duplicate model names collapse in the internal
    dictionaries but still count toward len(self._models)").
    """
    ordered = tuple(models)
    if not ordered:
        raise ValueError(f"REFUSED: {caller} needs at least one model")
    seen: dict[str, int] = {}
    for name in ordered:
        seen[name] = seen.get(name, 0) + 1
    duplicated = sorted(name for name, count in seen.items() if count > 1)
    if duplicated:
        raise ValueError(
            f"REFUSED: {caller} received duplicate model name(s) {duplicated}; "
            f"each model may appear at most once in {list(ordered)}"
        )
    return ordered


def mean_pinball(score: Mapping[str, Any]) -> dict[str, float]:
    """Mean pinball loss per component from one ledgered score payload.

    Averaging p10/p50/p90 scores the whole predictive band rather than the
    point alone -- the ensemble is blending distributions, so the weights
    have to be earned on distributions.
    """
    pinball = score["pinball"]
    return {
        component: float(np.mean([pinball[component][key] for key in QUANTILE_KEYS]))
        for component in COMPONENTS
    }


class ComponentWeights:
    """Exponentially-weighted inverse-loss blend weights per component.

    One weight per (model, component): a model can be the best in the room
    at ``close`` and the worst at ``high``, which a single per-model weight
    could not express.
    """

    def __init__(
        self,
        models: Sequence[str],
        *,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        floor: float = DEFAULT_FLOOR,
    ) -> None:
        self._models = _distinct_models(models, "ComponentWeights")
        if half_life_days <= 0.0:
            raise ValueError(f"REFUSED: half_life_days must be positive, got {half_life_days}")
        if floor < 0.0 or floor * len(self._models) >= 1.0:
            raise ValueError(
                f"REFUSED: floor {floor} leaves no room for {len(self._models)} models "
                "(floor * n_models must stay below 1)"
            )
        self._floor = float(floor)
        # Per-day decay factor: after exactly half_life_days of zero loss an
        # observation retains half its value.
        self._decay = 0.5 ** (1.0 / float(half_life_days))
        self._loss: dict[str, dict[str, float]] = {}

    def observe_day(self, losses: Mapping[str, Mapping[str, float]]) -> None:
        """Fold ONE revealed day's aggregated losses in.

        ``losses`` is ``{model: {component: that day's mean loss}}``. A
        model absent from the mapping is left completely untouched rather
        than decayed: decaying it would pull its loss toward zero, which in
        an INVERSE-loss scheme reads as "getting better", so a model that
        simply stopped forecasting would quietly take over the blend.
        """
        for model in self._models:
            per_component = losses.get(model)
            if per_component is None:
                continue
            state = self._loss.setdefault(model, {})
            for component in COMPONENTS:
                value = float(per_component[component])
                previous = state.get(component)
                # Seeded on the first observation rather than on zero, so a
                # cold start is not read as a perfect score.
                state[component] = value if previous is None else self._decay * previous + (1.0 - self._decay) * value

    def mean_loss(self, model: str) -> dict[str, float]:
        """This model's current exponentially-weighted loss per component."""
        state = self._loss.get(model, {})
        return {component: state.get(component, float("nan")) for component in COMPONENTS}

    def weights(self) -> dict[str, dict[str, float]]:
        """``{component: {model: weight}}``, non-negative and summing to 1."""
        resolved: dict[str, dict[str, float]] = {}
        for component in COMPONENTS:
            observed = {
                model: 1.0 / (self._loss[model][component] + _LOSS_EPS)
                for model in self._models
                if component in self._loss.get(model, {})
            }
            if not observed:
                # Nothing revealed yet: an equal blend is the only honest
                # prior a walk-forward run can hold on its first day.
                share = {model: 1.0 / len(self._models) for model in self._models}
            else:
                # A model with no history yet is treated as having the
                # AVERAGE LOSS of those that do -- neither rewarded nor
                # punished for being new (a late-joining model, or one
                # whose component was never scored).
                #
                # 1/mean(loss), NOT mean(1/loss): 1/x is convex, so the
                # mean of the strengths is dragged up hard by the best
                # model. Measured on losses of 0.001 and 0.1, mean(1/L)
                # implies a loss of 0.00198 -- better than every observed
                # model but one -- and hands a newcomer 0.333 of the blend
                # where it has earned 0.038 (Antigravity review: "Neutral
                # strength calculation for unobserved models").
                mean_loss = float(np.mean([self._loss[model][component] for model in observed]))
                neutral = 1.0 / (mean_loss + _LOSS_EPS)
                strengths = {model: observed.get(model, neutral) for model in self._models}
                total = sum(strengths.values())
                share = {model: strength / total for model, strength in strengths.items()}
            # floor + (1 - n*floor) * share keeps every model at or above the
            # floor while the weights still sum to exactly 1.
            headroom = 1.0 - self._floor * len(self._models)
            resolved[component] = {
                model: self._floor + headroom * value for model, value in share.items()
            }
        return resolved


def blend(
    forecasts: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    """Vincentized blend of several models' output for ONE (ticker, origin day).

    ``forecasts`` is ``{model: {"point": ..., "quantiles": ...}}`` -- the
    shape both an OHLCForecast and a ledgered score record already expose.
    Only the models actually present are blended, with their weights
    renormalized over that subset: coverage differs between models (a
    ticker below one model's warm-up, a refit day), and treating an absent
    model as a 0.0 forecast would drag the blend toward zero.
    """
    if not forecasts:
        raise ValueError("REFUSED: cannot blend an empty set of forecasts")
    point: dict[str, float] = {}
    bands: dict[str, dict[str, float]] = {}
    for component in COMPONENTS:
        per_model = weights[component]
        present = {model: float(per_model.get(model, 0.0)) for model in forecasts}
        total = sum(present.values())
        # Every present model weighted zero (possible only if a caller hands
        # in weights for models that all sat out) degrades to an equal
        # blend rather than a division by zero.
        share = (
            {model: value / total for model, value in present.items()}
            if total > 0.0
            else {model: 1.0 / len(forecasts) for model in forecasts}
        )
        point[component] = sum(
            share[model] * float(forecasts[model]["point"][component]) for model in forecasts
        )
        bands[component] = {
            key: sum(
                share[model] * float(forecasts[model]["quantiles"][component][key])
                for model in forecasts
            )
            for key in QUANTILE_KEYS
        }
    # Through the SAME shared post-processing every other model's output
    # passes through, rather than a private copy of the repairs. These are
    # NOT no-ops on valid inputs: per-component weights mean the components
    # are not combined under one convex coefficient vector, so two valid
    # bars can blend into an impossible one (see the module docstring).
    # Callers must surface `repairs` rather than discard it.
    repairs = RepairCounts()
    finalized = finalize_forecast(
        ticker="",
        origin_date="",
        target_date=None,
        point=point,
        quantiles=bands,
        ref_close=0.0,
        adj_ratio=1.0,
        repairs=repairs,
    )
    return {
        "point": dict(finalized.point),
        "quantiles": {component: dict(band) for component, band in finalized.quantiles.items()},
        "repairs": repairs.as_dict(),
    }


def _projected(record: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only what the blend needs, and fold the loss down at read time.

    A ledgered score record is mostly fields the ensemble never reads
    (quantiles_raw, and every scoring field but pinball). Since a whole
    year of them is resident at once, projecting here rather than storing
    the record wholesale is most of the memory this loop uses.
    """
    return {
        "point": record["point"],
        "quantiles": record["quantiles"],
        "actual": record["actual"],
        "ref_close": record["ref_close"],
        "adj_ratio": record["adj_ratio"],
        "seed": record.get("seed", 0),
        "losses": mean_pinball(record["score"]),
    }


def _emit_days(
    pooled: Mapping[tuple[str, str, str], dict[str, dict[str, Any]]],
    keys: Sequence[tuple[str, str, str]],
    models: tuple[str, ...],
    tracker: ComponentWeights,
) -> Iterator[dict[str, Any]]:
    """Blend and score ``keys``, grouped into target days in date order."""
    by_day: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for key in keys:
        by_day[key[0]].append(key)
    for target_date in sorted(by_day):
        # Read ONCE, before any of this day's records are blended: this
        # single ordering is what makes the whole thing walk-forward.
        weights = tracker.weights()
        day_losses: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for key in sorted(by_day[target_date]):
            entries = pooled[key]
            if len(entries) != len(models):
                # Intersect to shared (ticker, origin day) pairs, the rule
                # the multi-model reports already follow, so the ensemble's
                # numbers stay comparable with its components'.
                continue
            _, ticker, origin_date = key
            blended = blend(entries, weights)
            truth = entries[models[0]]
            yield {
                "schema_version": SCHEMA_VERSION,
                "record_type": "score",
                "model": ENSEMBLE_MODEL_NAME,
                "ticker": ticker,
                "origin_date": origin_date,
                "target_date": target_date,
                "seed": truth["seed"],
                "point": blended["point"],
                "quantiles": blended["quantiles"],
                # The ensemble has no pre-calibration band of its own: it
                # blends bands each already calibrated by their own model.
                "quantiles_raw": blended["quantiles"],
                # Counted, never silent: a per-component blend really can
                # need a bar repair, so the count rides out with the record
                # instead of being dropped on the floor.
                "repairs": blended["repairs"],
                "actual": truth["actual"],
                "ref_close": truth["ref_close"],
                "adj_ratio": truth["adj_ratio"],
                "score": score_forecast(
                    point=blended["point"],
                    quantiles=blended["quantiles"],
                    quantiles_raw=blended["quantiles"],
                    actual=truth["actual"],
                    ref_close=truth["ref_close"],
                ),
            }
            for model in models:
                for component, value in entries[model]["losses"].items():
                    day_losses[model][component].append(value)
        # Only NOW does the day this loop just blended reach the weights.
        if day_losses:
            tracker.observe_day(
                {
                    model: {component: float(np.mean(values)) for component, values in components.items()}
                    for model, components in day_losses.items()
                }
            )


def replay(
    run_dir: Path | str,
    models: Sequence[str],
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    floor: float = DEFAULT_FLOOR,
    read_fingerprints: dict[str, dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream ensemble score records over an existing run's ledger, causally.

    Yields records in the SAME shape harness._score_one() writes, under
    ENSEMBLE_MODEL_NAME, so metrics.py aggregates the ensemble beside the
    models it blends with no changes anywhere.

    Only (ticker, origin day) pairs EVERY requested model forecast are
    emitted -- the same intersect-to-shared-pairs rule the multi-model
    reports already follow, so the ensemble's numbers stay comparable with
    its components' rather than being computed over a different sample.

    Memory is bounded by ONE YEAR, not by the run. The ledger already
    shards by the origin date's year, so this walks those shards in order
    and holds only the year being processed. A record's target is its
    ticker's next trading row, so a forecast originating in year Y matures
    in Y or (across the new-year boundary) in Y+1 and no later; every pair
    whose target falls in Y is therefore complete once shard Y is loaded,
    and the rest carries forward. A full-universe multi-decade run is
    millions of records, so reading them all up front made peak usage scale
    with the whole replay (Antigravity review: "Memory footprint in
    stacking.replay for large backtests").

    ``read_fingerprints`` bounds each shard to the size the checkpoint
    recorded, the report-only reader's contract (see Ledger's docstring).
    """
    models = _distinct_models(models, "replay")
    ledger = Ledger(Path(run_dir), read_fingerprints=read_fingerprints)

    per_model_years = {model: ledger.available_years(model=model, kind="score") for model in models}
    for model, years in per_model_years.items():
        if not years:
            raise ValueError(
                f"REFUSED: no score records for model {model!r} under {ledger.root}; "
                "the ensemble cannot blend a model this run never scored"
            )
    years = sorted({year for shards in per_model_years.values() for year in shards})

    tracker = ComponentWeights(models, half_life_days=half_life_days, floor=floor)
    # Pairs whose target date lands past the shard year that produced them:
    # only ever the last trading day or two of a year, so this stays tiny.
    carried: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for year in years:
        pooled = carried
        for model in models:
            for record in ledger.iter_records(model=model, kind="score", year=year):
                key = (record["target_date"], record["ticker"], record["origin_date"])
                pooled.setdefault(key, {})[model] = _projected(record)
        # A later shard's origins are all in a later year, so their targets
        # are too: every pair maturing in this year or earlier is complete.
        ready = [key for key in pooled if key[0][:4] <= year]
        carried = {key: value for key, value in pooled.items() if key[0][:4] > year}
        yield from _emit_days(pooled, ready, models, tracker)
    yield from _emit_days(carried, list(carried), models, tracker)
