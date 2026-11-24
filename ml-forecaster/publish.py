"""Fail-closed guarded publish of ml-forecaster's per-ticker sizing suggestions.

**Shadow only -- this module no longer writes live sizing.** The sizing ladder is
still being determined by comparing several independent suggesters over time
(this one, ``wondrous-octopus``, ...), so by default the "published" file is
ml-forecaster's own incumbent record at ``<history root>/ml_forecaster/incumbent.json``
(``DEFAULT_CONFIG_PATH``), NOT ``config/ml_sizing_adjustment.json``, which stays
``inactive`` so the live factor is 1.0 until a human promotes a suggester after
review. Every run's suggestions are also appended to the shared, append-only
``sizing_suggestion_history`` (see ``docs/sizing_suggestion_history.md``). Passing
``config_path=`` explicitly still lets an operator publish to the live file
deliberately; nothing here does it by default. The rest of this docstring says
"the live config" for the file at ``config_path``, whichever it is.


Mirrors scripts/daily_per_ticker_calibration.py's refuse-and-say-why
pattern: a caller (train.py) hands this module a freshly trained model's
walk-forward evaluation and proposed per-ticker adjustments, and
guarded_publish() either atomically replaces the live
config/ml_sizing_adjustment.json or REFUSES and explains why, leaving the
existing file byte-for-byte untouched. It never raises for an ordinary
"this model isn't good enough" refusal -- that is the expected, common
outcome of a retrain, not an exceptional one; train.py's CLI turns a refusal
into a nonzero process exit code (see its own `status != "published"`
check), not a traceback.

Publish requires the new model's out-of-sample walk-forward MAE to be
STRICTLY BETTER than BOTH:

1. The currently-published model's own recorded `eval_metric.mae` -- read
   straight out of the live config/ml_sizing_adjustment.json (if it exists,
   has `status: "active"`, and actually carries one; an inactive or
   metric-less prior file imposes no floor, since there is then no
   currently-live model to regress against). This comparison is skipped
   ENTIRELY when the previously-published file's recorded
   `horizon_trading_days` doesn't match this run's (a different
   forward-return horizon lives on a different error scale), OR when its
   recorded `eval_metric.universe` doesn't match this run's
   `evaluation_universe` (a weekly retrain's expanding archive and
   possibly-changed active-ticker set means the incumbent's MAE was
   measured on a different walk-forward sample). When horizon/universe DO
   match but the recorded `eval_metric.evaluation_window` does NOT match
   this run's own walk-forward test-fold date range -- an IDENTICAL ticker
   set can still be scored over completely different calendar folds, since
   the archive gains new trading days every retrain, which is the common
   case -- raw MAEs are not compared directly; instead each run's SKILL
   RATIO (its own MAE relative to its own contemporaneous naive baseline)
   is compared, which cancels out most of a regime shift's distortion. See
   `_evaluation_window()`'s and `blocking_reasons()`'s own comments.
2. The better of THIS run's own two naive walk-forward baselines
   (predict-zero, predict-trailing-mean) -- publishing a model that can't
   even beat "always predict zero" over the same held-out folds would make
   config/ml_sizing_adjustment.json actively worse than a no-op.

A TIE on either check is treated as "not an improvement" and refused --
per CLAUDE.md's "Fail closed, always" invariant, an ambiguous result must
never silently replace a working, already-vetted model.

This is also the only place in ml-forecaster/ that touches
config/ml_sizing_adjustment.json -- train.py, model.py, evaluate.py, and
features.py never write to it directly.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeGuard

REPO_ROOT = Path(__file__).resolve().parent.parent
# Index 1, not 0 -- see features.py's identical comment on why (this
# subproject's own directory must stay ahead of the repo root on sys.path).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_fileio import atomic_write_text  # noqa: E402
from scripts.file_lock import cross_process_lock  # noqa: E402
from sizing_suggestion_history import history_root, record_suggestions  # noqa: E402

# ml-forecaster's own incumbent record, under the suggestion-history root (which
# honours $DCA_CRON_TRACKING_ROOT), NOT config/ml_sizing_adjustment.json: that
# file feeds live sizing (dca_config.load_ml_sizing_adjustment ->
# graded_buy_multiplier), and no suggester may write it by default while the
# operator is still comparing suggesters. Same file shape as before, so the
# regression gate's incumbent read (_read_current_config) is unchanged -- it just
# reads this suggester's own last publish instead of the shared live file.
DEFAULT_CONFIG_PATH = history_root() / "ml_forecaster" / "incumbent.json"
DEFAULT_GENERATED_BY = "ml-forecaster/publish.py"
DEFAULT_MAX_STALENESS_HOURS = 240
# Mirrors features.FORWARD_RETURN_HORIZON_DAYS -- duplicated here (not
# imported) so this module keeps its own light import footprint (no pandas/
# numpy just to publish a JSON file), matching the _SCHEMA_*_FLOOR/_CEIL
# constants below, which duplicate schemas/ml_sizing_adjustment.schema.json's
# bounds for the same reason. Read features.py, not this comment, as the
# source of truth.
DEFAULT_HORIZON_TRADING_DAYS = 5

# schemas/ml_sizing_adjustment.schema.json's own hard limits -- read that
# file, not this comment, as the source of truth; these are duplicated here
# ONLY so an out-of-range --min-multiplier/--max-multiplier CLI argument (or
# a caller passing them directly) is refused before ever attempting to
# write a file that would fail schema validation. jsonschema itself is not a
# runtime dependency of ml-forecaster (see requirements.txt) -- these bounds
# are checked by hand instead.
_SCHEMA_MIN_MULTIPLIER_FLOOR = 0.5
_SCHEMA_MIN_MULTIPLIER_CEIL = 1.0
_SCHEMA_MAX_MULTIPLIER_FLOOR = 1.0
_SCHEMA_MAX_MULTIPLIER_CEIL = 1.5
_SCHEMA_ADJUSTMENT_MIN = 0.5
_SCHEMA_ADJUSTMENT_MAX = 1.5


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that raises on a duplicate key in one JSON object.

    ``json.loads`` silently keeps the LAST occurrence of a duplicate object
    key by default -- a hand-edited or corrupted ``config/ml_sizing_adjustment.json``
    with two ``"eval_metric"`` blocks (or two entries anywhere else in the
    document) would otherwise be read back here with whichever one happened
    to come last silently winning. dca_config.load_ml_sizing_adjustment()
    -- the loader the LIVE strategy actually reads this same file through --
    already rejects this via its own identically-named hook, so a duplicate-
    keyed file would otherwise be read differently by the two readers: the
    live loader fails closed on it while THIS module (deciding whether to
    publish a new file over the old one) silently accepts a matching
    duplicate that isn't really there (Copilot review: "Reject duplicate
    JSON keys"). Duplicated rather than imported -- this module deliberately
    keeps no cross-import of the core config-parsing machinery (see
    features.py's identical convention for ticker-advisor/pricing.py).
    """
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON document")
        seen.add(key)
        result[key] = value
    return result


def _read_current_config(config_path: Path) -> dict[str, Any] | None:
    """The currently-published config, or None if absent/unreadable.

    "Unreadable" also covers syntactically valid JSON of the wrong top-level
    type (e.g. a bare ``[]`` or a string) -- blocking_reasons() below calls
    ``current.get(...)``, which would raise AttributeError on anything but a
    dict, crashing a scheduled retrain instead of treating a semantically
    unreadable file as "no valid prior model to regress against" (Codex
    review: "Reject non-object current publish files").

    Also covers a file whose bytes are not valid UTF-8 -- ``json.loads()``
    given raw bytes decodes them itself and can raise ``UnicodeDecodeError``,
    which is neither ``OSError`` nor ``json.JSONDecodeError`` and previously
    escaped this function uncaught, crashing the whole retrain over one
    damaged prior publish instead of the documented "no valid prior model"
    fallback (Copilot review: "Handle invalid UTF-8 in the current
    config"). And a duplicate JSON key, via ``_reject_duplicate_keys``
    above -- ``json.JSONDecodeError``'s superclass ``ValueError`` already
    covers the ``ValueError`` that hook raises, so no separate except clause
    is needed for it.
    """
    try:
        data = json.loads(config_path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_finite_number(value: Any) -> TypeGuard[float | int]:
    """True for a real int/float that isn't NaN/+-inf. Excludes bool (an int subclass)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


#: Rank-IC pass path (2026-11-11, operator decision): a model that does not beat the naive MAE baselines
#: may still publish if it reliably ORDERS dip days -- sizing only uses the ordering. Requires every
#: walk-forward fold's rank IC > 0, at least MIN_RANK_IC_FOLDS folds, and a mean >= MIN_MEAN_RANK_IC.
MIN_RANK_IC_FOLDS = 3
MIN_MEAN_RANK_IC = 0.02


def _rank_ic_passes(eval_summary: dict[str, Any]) -> bool:
    folds = eval_summary.get("folds")
    if not isinstance(folds, list) or len(folds) < MIN_RANK_IC_FOLDS:
        return False
    ics = [f.get("model_rank_ic") if isinstance(f, dict) else None for f in folds]
    if not all(_is_finite_number(v) and v > 0.0 for v in ics):
        return False
    return sum(ics) / len(ics) >= MIN_MEAN_RANK_IC


def _mean_rank_ic(eval_summary: dict[str, Any]) -> float | None:
    """Mean walk-forward rank IC, or None unless EVERY fold carries a finite one.

    Persisted as ``eval_metric.model_rank_ic`` so the next retrain can compare rank IC with the incumbent's
    (see the rank-IC incumbent comparison in ``blocking_reasons``).
    """
    folds = eval_summary.get("folds")
    if not isinstance(folds, list) or not folds:
        return None
    ics = [f.get("model_rank_ic") if isinstance(f, dict) else None for f in folds]
    if not all(_is_finite_number(v) for v in ics):
        return None
    return sum(ics) / len(ics)


def _baseline_mae(eval_summary: dict[str, Any]) -> float:
    """The better (lower) of this run's two naive walk-forward baseline MAEs.

    Requires BOTH baseline_zero_mae and baseline_mean_mae to be usable, not
    just at least one -- the module docstring's own documented requirement
    is "the BETTER of THIS run's own two naive walk-forward baselines",
    which is only knowable when both are actually available. Silently
    narrowing to whichever single baseline happens to be finite (an earlier,
    incomplete version of this fix) could publish a model that beats the
    one surviving baseline but would have FAILED the omitted one, had it
    been valid -- e.g. a corrupted baseline_zero_mae that would have been
    the harder (lower) bar to beat quietly drops out of the comparison
    instead of blocking the whole evaluation (Codex review:
    "Require both baseline metrics before publishing"). A NaN/infinite
    baseline compares false against every ">="/"<=" check in
    blocking_reasons() too, so treating it as "no floor" instead of
    "unusable, refuse" would silently impose no floor at all rather than
    blocking publish outright (Codex review: "Reject non-finite evaluation
    metrics before publishing").
    """
    overall = eval_summary.get("overall", {})
    baseline_zero_mae = overall.get("baseline_zero_mae")
    baseline_mean_mae = overall.get("baseline_mean_mae")
    if not _is_finite_number(baseline_zero_mae) or not _is_finite_number(baseline_mean_mae):
        raise ValueError(
            "eval_summary['overall'] carries no usable baseline_zero_mae/baseline_mean_mae -- "
            "BOTH must be finite, not just one"
        )
    return min(baseline_zero_mae, baseline_mean_mae)


def _evaluation_window(eval_summary: dict[str, Any]) -> tuple[tuple[str, str, str], ...] | None:
    """Every walk-forward fold's own (test_start, test_end,
    test_row_fingerprint) triple in this run, sorted for a deterministic
    fingerprint -- or None if `eval_summary["folds"]` is missing, empty, or
    malformed.

    Distinct from ``evaluation_universe`` (the ticker SET scored): two runs
    can score the identical ticker set while covering completely different
    calendar periods -- a weekly retrain's expanding archive gains new
    trading days every run, so even an identical universe/horizon means the
    incumbent's recorded MAE and a fresh candidate's MAE were measured over
    DIFFERENT walk-forward folds, not merely a different population (Codex
    review: "Compare models on the same evaluation dates"). A calmer or
    more volatile newly-added period can then make a genuinely worse
    candidate look better -- or block a real improvement -- purely from the
    sample change, the same incomparable-scale problem the universe check
    already guards against for a changed ticker set.

    Two earlier, both incomplete, fingerprints preceded this one:

    1. Recording only the overall (earliest test_start, latest test_end)
       span -- an archive correction that changes INTERIOR dip-day rows or
       per-ticker availability between two runs, without moving either
       endpoint date, changes which folds/observations were actually scored
       while leaving the recorded span identical (Codex review:
       "Fingerprint the actual evaluation observations").
    2. Recording every fold's own (test_start, test_end) BOUNDARY pair, not
       just the overall span -- still incomplete for the identical reason
       one level down: an archive correction that adds, removes, or changes
       an INTERIOR row within a fold's date range (e.g. a ticker's row
       within [test_start, test_end] gets dropped, or a newly-added
       ticker's row lands inside it) doesn't move that fold's own
       test_start/test_end either, so two runs' folds could still look
       identical while scoring genuinely different observations (Codex
       review: "Fingerprint observations rather than only fold endpoints").

    Each fold's own ``test_row_fingerprint`` (a SHA-256 digest of every test
    row's own (ticker, date) identity -- see evaluate.FoldResult's own
    docstring) closes this completely: two folds can only produce the same
    fingerprint by scoring the identical row SET, not merely a set that
    happens to share the same boundary dates. Sorted so this fingerprint
    doesn't depend on the order `eval_summary["folds"]` happens to list
    folds in.
    """
    folds = eval_summary.get("folds")
    if not isinstance(folds, list) or not folds:
        return None
    triples: list[tuple[str, str, str]] = []
    for fold in folds:
        if not isinstance(fold, dict):
            return None
        start, end = fold.get("test_start"), fold.get("test_end")
        row_fingerprint = fold.get("test_row_fingerprint")
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        if not isinstance(row_fingerprint, str) or not row_fingerprint:
            return None
        triples.append((start, end, row_fingerprint))
    return tuple(sorted(triples))


def _parse_persisted_evaluation_window(raw: Any) -> tuple[tuple[str, str, str], ...] | None:
    """Reconstruct a persisted `eval_metric["evaluation_window"]` back into
    the same shape `_evaluation_window()` produces, for direct comparison --
    or None if `raw` isn't shaped as a list of `[test_start, test_end,
    test_row_fingerprint]` string triples at all.

    A LEGACY published file recorded before the row-fingerprint field
    existed carries either the flat two-string span
    (`["2026-06-01", "2026-06-07"]`) or the boundary-pair-only shape
    (`[["2026-06-01", "2026-06-07"], ...]`) instead -- neither has a THIRD
    element per entry, so the `len(triple) != 3` check below fails on the
    very first one and this correctly returns None: unsafe to compare
    directly against the new fingerprinted window, exactly like every other
    unparseable prior window, not assumed compatible.
    """
    if not isinstance(raw, list) or not raw:
        return None
    triples: list[tuple[str, str, str]] = []
    for triple in raw:
        if (
            not isinstance(triple, list)
            or len(triple) != 3
            or not all(isinstance(v, str) and v for v in triple)
        ):
            return None
        triples.append((triple[0], triple[1], triple[2]))
    return tuple(sorted(triples))


def _incumbent_baseline_mae(eval_metric: dict[str, Any]) -> float | None:
    """The better (lower) of the CURRENTLY-PUBLISHED model's own recorded
    naive baseline MAEs, or None if either is missing/non-finite.

    Mirrors `_baseline_mae()`'s "require both, reject non-finite" contract,
    but reads from the persisted `eval_metric` dict a prior guarded_publish()
    call wrote, not a fresh `eval_summary`. Used only by the skill-ratio
    fallback in `blocking_reasons()` below.
    """
    baseline_zero_mae = eval_metric.get("baseline_zero_mae")
    baseline_mean_mae = eval_metric.get("baseline_mean_mae")
    if not _is_finite_number(baseline_zero_mae) or not _is_finite_number(baseline_mean_mae):
        return None
    return min(float(baseline_zero_mae), float(baseline_mean_mae))


def blocking_reasons(
    *,
    new_mae: float,
    eval_summary: dict[str, Any],
    current: dict[str, Any] | None,
    new_horizon_trading_days: int,
    new_evaluation_universe: list[str] | None = None,
    score_incumbent_model: Callable[[str], tuple[float, float] | None] | None = None,
) -> list[str]:
    """Reasons publish must be refused; an empty list means the new model may publish.

    `score_incumbent_model`, if given, is called with the currently-published
    model's own recorded `model_artifact_path` (a plain-JSON model.json --
    see model.TrainedModel) and must return a `(candidate_mae, incumbent_mae)`
    pair -- both computed over ONLY the ROWS genuinely out-of-sample for the
    incumbent (see evaluate.score_incumbent_on_folds(), the intended
    implementation, for why that restriction matters and why both sides
    must be computed over the identical held-out rows) -- or None if it
    can't be scored. Once this callback is applicable to a same-horizon
    incumbent with a persisted artifact, an unavailable or invalid result
    blocks publication rather than falling back to a weaker comparison.
    This module deliberately stays pandas/numpy-free
    (see the module docstring) -- the actual model-loading and re-scoring
    work happens on the caller's side of this callback, exactly like this
    repo's broker/data-provider injection pattern elsewhere
    (dca_live_adapters.py).
    """
    reasons: list[str] = []

    # Computed FIRST (not after the incumbent comparison below, where an
    # earlier version of this function computed it): the skill-ratio
    # fallback for a evaluation-window mismatch, just below, needs this
    # run's own baseline MAE too, not only the standalone baseline check at
    # the bottom of this function.
    try:
        baseline_mae = _baseline_mae(eval_summary)
    except ValueError as exc:
        baseline_mae = None
        baseline_mae_error = str(exc)
    else:
        baseline_mae_error = None

    if current is not None and current.get("status") == "active":
        eval_metric = current.get("eval_metric")
        previous_mae = eval_metric.get("mae") if isinstance(eval_metric, dict) else None
        previous_universe = eval_metric.get("universe") if isinstance(eval_metric, dict) else None
        previous_horizon = current.get("horizon_trading_days")
        previous_artifact_path = current.get("model_artifact_path")

        # Rank-IC incumbent comparison (2026-11-24): a candidate that only qualifies through the rank-IC pass
        # path (its MAE does not beat the naive baseline, see the bottom of this function) must not then be
        # held to the incumbent's MAE skill ratio -- it was never admitted on MAE, and an incumbent that
        # itself failed the MAE baseline sets a floor that no later rank-IC model can clear, so every
        # retrain stayed blocked_regression (2026-11-13 .. 11-23, 12 of 13 runs). Such a candidate is compared
        # on rank IC instead: strictly higher than the incumbent's recorded mean when it has one. An
        # incumbent that DID beat the MAE baseline keeps the full MAE comparison, so a weaker rank-IC-only
        # model can never displace a model that was admitted on error.
        new_rank_ic = _mean_rank_ic(eval_summary)
        previous_baseline_for_ic = _incumbent_baseline_mae(eval_metric) if isinstance(eval_metric, dict) else None
        incumbent_no_mae_skill = bool(
            _is_finite_number(previous_mae)
            and previous_baseline_for_ic is not None
            and float(previous_mae) >= previous_baseline_for_ic
        )
        compared_on_rank_ic = bool(
            baseline_mae is not None
            and new_mae >= baseline_mae
            and _rank_ic_passes(eval_summary)
            and previous_horizon == new_horizon_trading_days
            and incumbent_no_mae_skill
        )
        if compared_on_rank_ic:
            previous_rank_ic = eval_metric.get("model_rank_ic") if isinstance(eval_metric, dict) else None
            if _is_finite_number(previous_rank_ic) and new_rank_ic is not None and new_rank_ic <= previous_rank_ic:
                reasons.append(
                    f"new model's mean rank IC {new_rank_ic:.6f} is not strictly better than the "
                    f"currently-published model's recorded mean rank IC {float(previous_rank_ic):.6f}"
                )

        # The most rigorous comparison available: the CURRENTLY-PUBLISHED
        # model's own real, persisted coefficients (not merely its recorded
        # summary MAE) re-scored directly against the SAME genuinely
        # out-of-sample rows the candidate's own restricted MAE is computed
        # over -- sidestepping the whole "did the two runs' evaluation
        # windows happen to line up" question the universe/window checks
        # below exist to answer for the weaker raw-metric comparison (Codex
        # review: "Compare the incumbent on the current evaluation folds" --
        # this was previously left as a documented, deliberately-unbuilt
        # limitation; see evaluate.score_incumbent_on_folds()'s own
        # docstring, including why comparing against THIS run's full-dataset
        # new_mae, rather than a candidate MAE restricted to the identical
        # held-out rows, would still be unsound -- Codex review: "Avoid
        # scoring the incumbent on its training rows"). Preferred over BOTH
        # the window-match branch and
        # the skill-ratio fallback whenever it succeeds, since it's strictly
        # the more direct measurement either way. Still gated on a matching
        # horizon: the incumbent's predictions are calibrated to ITS OWN
        # forward-return horizon, and comparing them against a target
        # computed for a DIFFERENT horizon compares two different scales
        # regardless of the rows being identical.
        rescored: tuple[float, float] | None = None
        rescoring_required = (
            score_incumbent_model is not None
            and isinstance(previous_artifact_path, str)
            and previous_artifact_path
            and previous_horizon == new_horizon_trading_days
            and not compared_on_rank_ic
        )
        if rescoring_required:
            try:
                rescored = score_incumbent_model(previous_artifact_path)
            except Exception:
                # Treat I/O and reconstruction failures like an unavailable
                # holdout below.  Falling back would silently weaken the gate
                # precisely when the shared out-of-sample comparison is
                # required to protect the active incumbent.
                rescored = None
            if rescored is not None and not (
                isinstance(rescored, tuple)
                and len(rescored) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in rescored)
            ):
                rescored = None

        if rescored is not None:
            restricted_candidate_mae, restricted_incumbent_mae = rescored
            if restricted_candidate_mae >= restricted_incumbent_mae:
                reasons.append(
                    f"new model's out-of-sample MAE {restricted_candidate_mae:.6f}, restricted to rows "
                    "genuinely out-of-sample for the incumbent, is not strictly better than the "
                    f"currently-published model's own re-scored MAE {restricted_incumbent_mae:.6f} over "
                    "that same restricted row set (the incumbent's real persisted weights evaluated on "
                    "data it was not trained on)"
                )
        elif rescoring_required:
            reasons.append(
                "cannot compare the new model with the currently-published model on a shared "
                "out-of-sample holdout; refusing to replace the incumbent until enough post-cutoff "
                "history is available and both models can be scored"
            )
        elif compared_on_rank_ic:
            # Compared on rank IC above; the MAE comparisons below do not apply to this candidate.
            pass
        else:
            # A currently-published model trained on a DIFFERENT forward-return
            # horizon has an MAE on an incomparable scale -- a 1-day-return model
            # can look strictly "better" than a 5-day-return model purely
            # because one-day returns have a smaller natural error scale, not
            # because it is actually a better sizing model. Treat a horizon
            # mismatch the SAME as "no eval_metric at all": no floor to beat.
            # This also covers an older published file that predates this field
            # entirely (previous_horizon is None, which can never equal an int
            # new_horizon_trading_days) -- an unknown horizon is exactly as
            # unsafe to compare against as a known-different one.
            #
            # The evaluated TICKER UNIVERSE is the same kind of incomparable-
            # scale problem: weekly retrains use an expanding archive and can
            # score a changed active-ticker set, so the incumbent's recorded MAE
            # was measured on an older, different walk-forward sample -- an
            # easier evaluation population can make a genuinely worse model look
            # numerically better (or a harder one look worse) purely from the
            # sample change, not from real skill. Compared as SETS (order-
            # independent) rather than the exact recorded list, since the
            # underlying concern is which tickers were scored, not what order
            # they were recorded in (Codex review: "Compare models on the same
            # evaluation sample"). A legacy published file recorded before this
            # field existed (previous_universe is None) is treated the same as
            # an unknown horizon above -- unsafe to compare, not assumed
            # compatible.
            universe_matches = (
                isinstance(previous_universe, list)
                and new_evaluation_universe is not None
                and set(previous_universe) == set(new_evaluation_universe)
            )
            # The evaluation DATE WINDOW is a separate incomparable-scale
            # problem from the ticker universe above: an identical ticker set
            # can still be scored over completely different calendar folds,
            # since a weekly retrain's expanding archive adds new trading days
            # every run. Comparing raw MAEs anyway lets a calmer or more
            # volatile newly-added period make a genuinely worse candidate look
            # better (or block a real improvement) purely from the sample
            # change, not from actual model skill -- see _evaluation_window()'s
            # own docstring. A legacy published file recorded before this field
            # existed (previous_window is None) is treated the same as an
            # unknown horizon/universe above -- unsafe to compare directly, not
            # assumed compatible.
            previous_window_raw = eval_metric.get("evaluation_window") if isinstance(eval_metric, dict) else None
            previous_window = _parse_persisted_evaluation_window(previous_window_raw)
            new_window = _evaluation_window(eval_summary)
            window_matches = (
                previous_window is not None and new_window is not None and previous_window == new_window
            )
            if (
                isinstance(eval_metric, dict)
                and _is_finite_number(previous_mae)
                and previous_horizon == new_horizon_trading_days
                and universe_matches
            ):
                previous_mae_float = float(previous_mae)
                if window_matches:
                    # Exact comparison: both MAEs were measured over the
                    # IDENTICAL held-out observations -- the most rigorous
                    # check available among these three when the incumbent's
                    # own model artifact isn't loadable/re-scorable.
                    if new_mae >= previous_mae_float:
                        reasons.append(
                            f"new model out-of-sample MAE {new_mae:.6f} is not strictly better than the "
                            f"currently-published model's recorded MAE {previous_mae_float:.6f}"
                        )
                elif baseline_mae is not None:
                    # Fold windows differ -- the common case, since the archive
                    # advances every retrain, and no re-scorable incumbent
                    # artifact was available above. Simply skipping the
                    # incumbent comparison here (an earlier version of this
                    # fix) silently neutered it for the normal weekly cadence:
                    # a candidate that regresses from the incumbent could
                    # still publish as long as it beat the naive baselines,
                    # since nothing else blocked it (Codex review: "Re-score
                    # the incumbent when evaluation dates advance"). Compare
                    # each run's SKILL RATIO instead -- its own MAE relative
                    # to its own contemporaneous naive baseline -- rather than
                    # raw MAE: a regime shift (a calmer or more volatile
                    # newly-added period) moves a fold's model MAE and its
                    # naive-baseline MAE in the SAME direction together, so
                    # the ratio between them is far more stable across
                    # different fold windows than either MAE alone. Requiring
                    # the new run's ratio to be strictly better keeps a
                    # genuine regression check in place instead of dropping to
                    # "no floor at all" for what is, in practice, every
                    # ordinary retrain whose incumbent artifact has gone
                    # missing (e.g. cleaned up, or published before this
                    # module recorded model_artifact_path at all).
                    previous_baseline_mae = (
                        _incumbent_baseline_mae(eval_metric) if isinstance(eval_metric, dict) else None
                    )
                    # Both denominators must be STRICTLY positive, not merely
                    # finite (_incumbent_baseline_mae()/_baseline_mae() already
                    # guarantee finite) -- a naive baseline MAE of exactly 0.0 is
                    # a real, if degenerate, possibility (every held-out target
                    # in that run was itself exactly zero) and would otherwise
                    # raise an uncaught ZeroDivisionError from either ratio
                    # below, aborting the entire guarded_publish() call instead
                    # of failing closed the same way an unusable
                    # previous_baseline_mae (None) already does (Codex review:
                    # "Reject zero baselines before computing skill ratios").
                    # Falling back to "no floor" here only skips THIS incumbent
                    # skill-ratio comparison -- the unconditional
                    # `new_mae >= baseline_mae` check at the bottom of this
                    # function still runs and, since MAE can never be negative,
                    # correctly blocks publish outright whenever baseline_mae
                    # itself is 0.0 (no model can be "strictly better" than a
                    # perfect baseline).
                    if previous_baseline_mae is not None and previous_baseline_mae > 0.0 and baseline_mae > 0.0:
                        previous_skill_ratio = previous_mae_float / previous_baseline_mae
                        new_skill_ratio = new_mae / baseline_mae
                        if new_skill_ratio >= previous_skill_ratio:
                            reasons.append(
                                f"new model's skill ratio {new_skill_ratio:.6f} (MAE {new_mae:.6f} / baseline "
                                f"{baseline_mae:.6f}) is not strictly better than the currently-published "
                                f"model's recorded skill ratio {previous_skill_ratio:.6f} (MAE "
                                f"{previous_mae_float:.6f} / baseline {previous_baseline_mae:.6f}) -- evaluation "
                                f"windows differ ({new_window} vs {previous_window}), so raw MAEs are "
                                "compared via each run's own relative skill instead"
                            )

    if baseline_mae is None:
        reasons.append(baseline_mae_error or "unable to determine baseline MAE")
    elif new_mae >= baseline_mae and not _rank_ic_passes(eval_summary):
        reasons.append(
            f"new model out-of-sample MAE {new_mae:.6f} is not strictly better than the naive "
            f"walk-forward baseline MAE {baseline_mae:.6f} (best of predict-zero / predict-trailing-mean), "
            f"and the rank-IC pass path (every fold > 0, >= {MIN_RANK_IC_FOLDS} folds, mean >= "
            f"{MIN_MEAN_RANK_IC}) is not met either"
        )

    return reasons


def _validate_bounds(min_multiplier: float, max_multiplier: float) -> list[str]:
    problems = []
    # bool is a subclass of int in Python, so `True`/`False` pass every
    # chained numeric comparison below unchanged (e.g. min_multiplier=True
    # reads as exactly 1.0, which sits comfortably inside [0.5, 1.0]) --
    # rejected explicitly here for the same reason every other numeric
    # boundary in this codebase excludes bool first. Left unrejected, the
    # config loader's own runtime check (dca_config.py's load_ml_sizing_
    # adjustment(), which DOES reject a boolean bound) discards the whole
    # publication as malformed, silently disabling every previously-active
    # sizing adjustment even though guarded_publish() itself reported
    # "published" (Codex review: "Reject boolean multiplier bounds before
    # publishing").
    #
    # A caller passing a genuinely NON-numeric bound -- a string like
    # "0.8", None, a list -- isn't a bool either, so it survived the check
    # above unchanged and reached the chained comparisons below, which raise
    # an uncaught TypeError ('<=' not supported between instances of ...)
    # instead of the documented blocked_invalid_bounds result -- this
    # function's own type annotation (`float`) is not enforced at runtime
    # for a direct guarded_publish() caller that doesn't go through
    # train.py's own argparse `type=float` validation (Codex review:
    # "Validate multiplier bound types before comparing"). Checked with the
    # same _is_finite_number() helper already used for every other numeric
    # boundary in this module (staleness, adjustment values).
    if not _is_finite_number(min_multiplier):
        problems.append(f"min_multiplier {min_multiplier!r} must be a finite number")
    if not _is_finite_number(max_multiplier):
        problems.append(f"max_multiplier {max_multiplier!r} must be a finite number")
    if problems:
        return problems
    if not (_SCHEMA_MIN_MULTIPLIER_FLOOR <= min_multiplier <= _SCHEMA_MIN_MULTIPLIER_CEIL):
        problems.append(
            f"min_multiplier {min_multiplier} outside the schema's allowed "
            f"[{_SCHEMA_MIN_MULTIPLIER_FLOOR}, {_SCHEMA_MIN_MULTIPLIER_CEIL}]"
        )
    if not (_SCHEMA_MAX_MULTIPLIER_FLOOR <= max_multiplier <= _SCHEMA_MAX_MULTIPLIER_CEIL):
        problems.append(
            f"max_multiplier {max_multiplier} outside the schema's allowed "
            f"[{_SCHEMA_MAX_MULTIPLIER_FLOOR}, {_SCHEMA_MAX_MULTIPLIER_CEIL}]"
        )
    if min_multiplier > max_multiplier:
        problems.append(f"min_multiplier {min_multiplier} exceeds max_multiplier {max_multiplier}")
    return problems


def _validate_staleness(max_staleness_hours: float) -> list[str]:
    """schemas/ml_sizing_adjustment.schema.json requires `exclusiveMinimum: 0` on max_staleness_hours.

    Checked by hand here for the same reason _validate_bounds() is: publish.py
    doesn't take jsonschema as a runtime dependency (see module docstring),
    so an out-of-schema value must be refused before ever reaching
    json.dumps()/atomic_write_text(), not discovered later by a downstream
    schema-validating reader. A non-finite (NaN/inf) or nonpositive value is
    refused outright -- load_ml_sizing_adjustment() in the core repo treats a
    schema-invalid max_staleness_hours as an immediately-stale config,
    silently disabling every adjustment, which is exactly the failure mode
    this guards against.
    """
    problems = []
    if not isinstance(max_staleness_hours, (int, float)) or isinstance(max_staleness_hours, bool):
        problems.append(f"max_staleness_hours {max_staleness_hours!r} must be a finite positive number")
    elif not math.isfinite(max_staleness_hours) or max_staleness_hours <= 0:
        problems.append(
            f"max_staleness_hours {max_staleness_hours} must be a finite positive number "
            "(schema requires exclusiveMinimum: 0)"
        )
    return problems


def _validate_adjustments(adjustments: dict[str, float], min_multiplier: float, max_multiplier: float) -> list[str]:
    problems = []
    # An EMPTY adjustments mapping trivially passes every per-key check
    # below (both loops are simply no-ops over nothing) -- but publishing it
    # anyway writes a fresh, "status": "active" config whose "adjustments"
    # is {}, silently REMOVING every previously-active live sizing override
    # rather than refusing. train.py's own CLI already refuses this after
    # its own staleness-snapshot filtering ("no fresh feature snapshot
    # survived..."), but that refusal lives entirely in the CLI -- this
    # shared publication boundary itself stayed fail-OPEN for any OTHER
    # caller that reaches guarded_publish() directly with an empty mapping
    # (Codex review: "Refuse active publications with no adjustments").
    if not adjustments:
        problems.append("adjustments must not be empty -- refusing to publish a config with no live overrides")
        return problems
    # The write path below normalizes every key with `ticker.upper()` --
    # distinct input keys that normalize to the SAME symbol (e.g. "AAPL" and
    # "aapl") silently collapse into one dict entry there, with whichever
    # value iteration happens to visit last winning while guarded_publish()
    # still reports "published". Caught here, before that write, rather than
    # relying on the write path to notice (Codex review: "Reject case-folded
    # adjustment keys before publishing").
    seen_upper: dict[str, str] = {}
    for ticker in adjustments:
        # A non-string key (e.g. a direct caller passing {1: 1.1}) can't be
        # normalized here at all -- and the write path's own unconditional
        # `ticker.upper()` (see guarded_publish()'s payload construction)
        # would raise an uncaught AttributeError on it instead of producing
        # the documented blocked_invalid_adjustments result (Codex review:
        # "Reject non-string adjustment keys"). Flagged directly, before
        # ever attempting to normalize it for the case-fold check below.
        if not isinstance(ticker, str):
            problems.append(f"adjustment key {ticker!r} must be a string")
            continue
        upper = ticker.upper()
        if upper in seen_upper:
            problems.append(f"adjustment keys {seen_upper[upper]!r} and {ticker!r} both normalize to {upper!r}")
        else:
            seen_upper[upper] = ticker
    for ticker, value in adjustments.items():
        if not isinstance(ticker, str):
            continue  # already reported above; avoid a duplicate/misleading message here
        # A direct (non-CLI) guarded_publish() caller isn't constrained to
        # pass genuine floats -- a string or None raises an uncaught
        # TypeError from the range comparisons below instead of producing
        # the documented blocked_invalid_adjustments result, and a bool
        # (int subclass) passes the comparisons as exactly 0.0/1.0 and gets
        # published, silently replacing any prior live adjustment for that
        # ticker (Codex review: "Validate adjustment types before comparing
        # their bounds").
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            problems.append(f"{ticker} adjustment {value!r} must be a finite, non-boolean number")
            continue
        if not (_SCHEMA_ADJUSTMENT_MIN <= value <= _SCHEMA_ADJUSTMENT_MAX):
            problems.append(
                f"{ticker} adjustment {value} outside the schema's hard [{_SCHEMA_ADJUSTMENT_MIN}, "
                f"{_SCHEMA_ADJUSTMENT_MAX}] range"
            )
        elif not (min_multiplier <= value <= max_multiplier):
            problems.append(
                f"{ticker} adjustment {value} outside this run's own declared bounds "
                f"[{min_multiplier}, {max_multiplier}]"
            )
    return problems


def guarded_publish(
    *,
    adjustments: dict[str, float],
    eval_summary: dict[str, Any],
    model_version: str,
    config_path: Path = DEFAULT_CONFIG_PATH,
    min_multiplier: float = 0.8,
    max_multiplier: float = 1.2,
    max_staleness_hours: float = DEFAULT_MAX_STALENESS_HOURS,
    horizon_trading_days: int = DEFAULT_HORIZON_TRADING_DAYS,
    evaluation_universe: list[str] | None = None,
    generated_at: datetime | None = None,
    generated_by: str = DEFAULT_GENERATED_BY,
    model_artifact_path: str | None = None,
    score_incumbent_model: Callable[[str], tuple[float, float] | None] | None = None,
    history_dir: Path | None = None,
) -> dict[str, Any]:
    """Publish a new incumbent file (and history record), or refuse and say why.

    `history_dir` overrides the suggestion-history root the run's suggestions are
    appended to (default: sizing_suggestion_history.history_root()). Every run
    that gets past input validation is recorded -- ``published`` and
    ``blocked_regression`` alike -- because a model the gate keeps rejecting is
    itself evidence for the later suggester comparison.

    `horizon_trading_days` is the forward-return horizon THIS model was
    trained on (train.py's `--horizon`) -- persisted into the payload so the
    NEXT retrain's regression check (see blocking_reasons()) can tell whether
    it is comparable to the currently-published model's recorded MAE at all.
    A run trained on a different horizon is never compared against the prior
    model's MAE, since the two MAEs live on different scales.

    `evaluation_universe` is the (order-independent) set of tickers THIS
    run's walk-forward evaluation actually scored -- also persisted, and
    also required to match before the prior model's MAE is treated as a
    floor. A weekly retrain's expanding archive and possibly-changed active
    universe means the incumbent's recorded MAE was measured on a different
    sample; comparing anyway could let an easier evaluation population make
    a genuinely worse model look numerically better (Codex review: "Compare
    models on the same evaluation sample").

    `model_artifact_path` is where THIS run's own trained model.json lives
    (train.py's models/<date>/model.json) -- persisted into the payload so a
    FUTURE retrain can locate and re-score THIS model as the incumbent (see
    `score_incumbent_model` next). `score_incumbent_model`, if given, is
    called with the CURRENTLY-published model's own recorded
    `model_artifact_path` and must return a `(candidate_mae, incumbent_mae)`
    pair, both restricted to walk-forward folds genuinely out-of-sample for
    the incumbent (see blocking_reasons()'s own docstring, and
    evaluate.score_incumbent_on_folds() for the intended implementation and
    why that restriction matters) -- the most rigorous available comparison,
    superseding the window-match/skill-ratio comparisons whenever it
    succeeds.

    Returns a result dict with at least a "status" key:
      - "published": the file was written; "config_path"/"new_model_mae" are
        also present.
      - "blocked_regression": the new model didn't beat the currently-
        published model and/or the naive baseline; "reasons" lists why.
      - "blocked_invalid_bounds" / "blocked_invalid_adjustments" /
        "blocked_invalid_staleness": the caller-supplied bounds, adjustments,
        or max_staleness_hours don't fit the schema's own hard limits;
        "reasons" lists why. Checked BEFORE the regression comparison, since
        an out-of-schema publish must never happen regardless of how good
        the model is.
      - "blocked_missing_eval_metric": `eval_summary` doesn't carry a usable
        `overall.model_mae` to compare with -- refuses rather than guessing.

    On any "blocked_*" status, config_path is left completely untouched
    (never even opened for writing).
    """
    resolved_generated_at = generated_at or datetime.now(timezone.utc)
    if resolved_generated_at.tzinfo is None:
        resolved_generated_at = resolved_generated_at.replace(tzinfo=timezone.utc)

    bound_problems = _validate_bounds(min_multiplier, max_multiplier)
    if bound_problems:
        return {"status": "blocked_invalid_bounds", "reasons": bound_problems}

    adjustment_problems = _validate_adjustments(adjustments, min_multiplier, max_multiplier)
    if adjustment_problems:
        return {"status": "blocked_invalid_adjustments", "reasons": adjustment_problems}

    staleness_problems = _validate_staleness(max_staleness_hours)
    if staleness_problems:
        return {"status": "blocked_invalid_staleness", "reasons": staleness_problems}

    overall = eval_summary.get("overall", {})
    new_mae = overall.get("model_mae")
    # NaN/inf pass isinstance(..., (int, float)) -- and Python's json module
    # happily round-trips them into the persisted eval_metric.mae, where
    # every downstream ">="/"<=" regression comparison against them compares
    # false, silently waving a broken evaluation through the gate instead of
    # blocking it (Codex review: "Reject non-finite evaluation metrics
    # before publishing").
    if not _is_finite_number(new_mae):
        return {
            "status": "blocked_missing_eval_metric",
            "reasons": ["eval_summary['overall']['model_mae'] is missing, non-numeric, or non-finite"],
        }

    # The read (for the regression comparison), the comparison itself, and
    # the write must all happen under one lock: two overlapping retrains
    # could otherwise both read the same currently-published MAE, both pass
    # the regression gate independently, and then race to write -- letting a
    # worse model overwrite a better one that published in between (Codex
    # review: "Make the regression check and publish atomic").
    lock_path = config_path.parent / f"{config_path.name}.lock"
    with cross_process_lock(lock_path):
        current = _read_current_config(config_path)
        reasons = blocking_reasons(
            new_mae=new_mae,
            eval_summary=eval_summary,
            current=current,
            new_horizon_trading_days=horizon_trading_days,
            new_evaluation_universe=evaluation_universe,
            score_incumbent_model=score_incumbent_model,
        )
        # A non-finite metric (a blocked run's inf/NaN baseline is exactly what the gate
        # exists to catch) must not crash the record -- or make the history writer, which
        # refuses non-strict JSON, turn a clean "blocked_*" result into a traceback.
        history_metrics = {
            key: (value if _is_finite_number(value) else None)
            for key, value in {
                "mae": new_mae,
                "rmse": overall.get("model_rmse"),
                "hit_rate": overall.get("model_hit_rate"),
                "baseline_zero_mae": overall.get("baseline_zero_mae"),
                "baseline_mean_mae": overall.get("baseline_mean_mae"),
            }.items()
        }

        def _record(status: str) -> None:
            record_suggestions(
                "ml_forecaster",
                adjustments,
                status=status,
                model_version=model_version,
                bounds={"min_multiplier": min_multiplier, "max_multiplier": max_multiplier},
                generated_at=resolved_generated_at,
                source={
                    "generated_by": generated_by,
                    "horizon_trading_days": horizon_trading_days,
                    "model_artifact_path": model_artifact_path,
                    "evaluation_universe": sorted({t.upper() for t in evaluation_universe})
                    if evaluation_universe is not None
                    else None,
                },
                metrics=history_metrics,
                root=history_dir,
            )

        if reasons:
            _record("blocked_regression")
            return {"status": "blocked_regression", "reasons": reasons, "new_model_mae": new_mae}

        eval_window = _evaluation_window(eval_summary)
        payload = {
            "$schema": "../schemas/ml_sizing_adjustment.schema.json",
            "version": 1,
            "status": "active",
            "generated_by": generated_by,
            "generated_at": resolved_generated_at.isoformat(),
            "model_version": model_version,
            "max_staleness_hours": max_staleness_hours,
            "horizon_trading_days": horizon_trading_days,
            # Not part of the schema's required fields, but additionalProperties
            # is true -- read back by blocking_reasons() above on the NEXT
            # retrain (via score_incumbent_model, see this function's own
            # docstring) to locate and re-score THIS run's model as the
            # incumbent. None (no path passed, e.g. a caller that doesn't wire
            # this feature) round-trips through JSON as null, which fails the
            # `isinstance(previous_artifact_path, str)` check there and is
            # correctly treated as "not re-scorable" rather than a crash.
            "model_artifact_path": model_artifact_path,
            "bounds": {"min_multiplier": min_multiplier, "max_multiplier": max_multiplier},
            "adjustments": {ticker.upper(): round(value, 6) for ticker, value in adjustments.items()},
            # Not part of the schema's required fields, but additionalProperties
            # is true -- this is the "currently-published model's own recorded
            # eval metric" the NEXT retrain's guarded_publish() call reads back
            # via _read_current_config() to decide whether IT is an improvement.
            "eval_metric": {
                "mae": new_mae,
                "rmse": overall.get("model_rmse"),
                "hit_rate": overall.get("model_hit_rate"),
                "baseline_zero_mae": overall.get("baseline_zero_mae"),
                "baseline_mean_mae": overall.get("baseline_mean_mae"),
                # Mean walk-forward rank IC, read back by the NEXT retrain's rank-IC incumbent comparison.
                "model_rank_ic": _mean_rank_ic(eval_summary),
                # Read back by blocking_reasons() above on the NEXT retrain --
                # None (evaluation_universe not passed) round-trips through
                # JSON as null, which fails `isinstance(previous_universe,
                # list)` there and is correctly treated as unsafe-to-compare,
                # the same as a legacy file that predates this field.
                "universe": sorted({t.upper() for t in evaluation_universe})
                if evaluation_universe is not None
                else None,
                # Read back by blocking_reasons() above on the NEXT retrain,
                # via _parse_persisted_evaluation_window() -- see
                # _evaluation_window()'s own docstring for why an identical
                # ticker universe is not enough on its own: the walk-forward
                # folds' own scored OBSERVATIONS must match too, not just
                # the overall span or even each fold's own boundary dates.
                # Each fold's own (test_start, test_end, test_row_fingerprint)
                # triple is serialized as a 3-element list -- JSON has no
                # tuple type -- so this round-trips through
                # _parse_persisted_evaluation_window() unchanged.
                "evaluation_window": (
                    [list(triple) for triple in eval_window] if eval_window is not None else None
                ),
            },
        }
        atomic_write_text(config_path, json.dumps(payload, indent=2) + "\n")
        _record("published")
        return {"status": "published", "new_model_mae": new_mae, "config_path": str(config_path)}


__all__ = ["DEFAULT_CONFIG_PATH", "blocking_reasons", "guarded_publish"]
