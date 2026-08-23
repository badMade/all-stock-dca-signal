#!/usr/bin/env python3
"""CLI: train + walk-forward-evaluate + guarded-publish the pooled ML sizing model.

Meant to be invoked by an external scheduled agent session on a weekly
cadence (e.g. after Friday close) -- this repo makes no network calls and
has no in-repo scheduler (CLAUDE.md's "This is a library driven from
outside, not a self-contained app" / "The live scheduled runs are executed
by external agent sessions" framing applies here too), so this file
deliberately contains no cron/scheduling code of its own. It is only the CLI
such an external cadence calls; scripts/daily_per_ticker_calibration.py and
scripts/generate_per_ticker_sizing.py follow the identical pattern for the
existing (non-ML) per-ticker sizing calibration.

Usage:
    python train.py
        # With no --tickers, trains on config/tickers.json's "active" universe
        # only (filtered to whichever of those have an archived CSV) -- NOT
        # every CSV in the archive. The archive-maintenance flow adds/refreshes
        # active histories but does not delete a ticker's CSV when it is later
        # excluded from the universe, so an unfiltered glob would keep pooling
        # stale/excluded tickers into the shared Ridge coefficients that also
        # drive the live universe's published adjustments.
    python train.py --tickers AAPL MSFT GOOGL --n-splits 4 --alpha 2.0
    python train.py --all-tickers-in-archive
        # Opt-in: discover every *.csv under --history-dir, ignoring
        # config/tickers.json entirely (the pre-existing behavior).
    python train.py --dry-run
        # Train + walk-forward-evaluate + write models/<date>/model.json and
        # eval_report.{json,md}; never touches config/ml_sizing_adjustment.json.
    python train.py --history-dir /path/to/ticker_data --output /path/to/config/ml_sizing_adjustment.json

Exit code is 0 only when a new model was actually published (or --dry-run
completed); any refusal (insufficient data, a walk-forward MAE that doesn't
beat the currently-published model or the naive baseline, out-of-schema
bounds/adjustments) returns 1 -- see publish.guarded_publish()'s own
docstring for the full set of "blocked_*" statuses this can surface.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pandas as pd

import evaluate as evaluate_module
import model as model_module
import publish as publish_module
from features import (
    _TICKER_SYMBOL_PATTERN,
    build_dataset,
    build_feature_frame,
    configured_ticker_history_dir,
    discover_available_tickers,
)

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
# Index 1, not 0 -- see features.py's identical comment on why (this
# subproject's own directory must stay ahead of the repo root on sys.path).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_config import get_config_dir  # noqa: E402
from dca_paths import stream_root  # noqa: E402

# Honours $DCA_CRON_TRACKING_ROOT via dca_paths.stream_root(), the same
# artifact-root convention every other stream in this repo uses -- the
# module-directory default this replaced ignored that override entirely and
# wrote inside the (potentially read-only, e.g. an installed wheel's
# site-packages) source tree instead of the configured artifact tree
# (AGENTS.md: never hard-code a local artifact path; Codex review: "Route
# model artifacts through the shared artifact root").
DEFAULT_MODELS_DIR = Path(stream_root("ml_forecaster")) / "models"
DEFAULT_CONFIG_PATH = publish_module.DEFAULT_CONFIG_PATH
#: The active DCA config directory's tickers.json -- the source of truth for
#: the live DCA universe (CLAUDE.md's "Config <-> constant parity"
#: invariant). Resolved through dca_config.get_config_dir() (honours
#: $DCA_CONFIG_DIR), not a REPO_ROOT-relative literal -- see
#: publish.DEFAULT_CONFIG_PATH's own comment for why a forecaster default
#: must track the SAME config directory the live strategy actually reads
#: from, not silently diverge onto a checkout-relative path (Codex review:
#: "Honor the active config directory in forecaster defaults"). Still read
#: directly with stdlib json below rather than importing near_close_dca's
#: ACTIVE_TICKERS constant or calling load_strategy_config() -- only the
#: DIRECTORY resolution is shared with the core tree, not its config-parsing
#: machinery (see features.py's module docstring on the identical choice for
#: pricing.py).
DEFAULT_TICKERS_CONFIG_PATH = get_config_dir() / "tickers.json"

#: Imported from features.py above -- _load_active_tickers below validates
#: against it directly rather than loading schemas/tickers.schema.json (this
#: sibling tool has no jsonschema dependency, matching its own "no
#: cross-import of the core stdlib-only tree" convention). features.py
#: already needs the identical pattern to validate an explicit --tickers
#: list before joining it into a filesystem path (Codex review: "Validate
#: explicit ticker symbols before resolving CSV paths") -- importing it here
#: keeps a single source of truth instead of two copies silently drifting
#: apart.

#: Below this many pooled dip-day rows, a walk-forward evaluation is close
#: to meaningless (a handful of rows per fold) even if it technically runs --
#: refuse outright rather than publish a model "evaluated" on noise.
MINIMUM_DIP_DAY_ROWS = 30


def _positive_int(raw: str) -> int:
    """argparse `type=` validator for --horizon: strictly positive integers only.

    A zero or negative horizon makes features.compute_forward_return_target()
    and features.compute_label_end_date() shift BACKWARD instead of forward
    (both are a plain `.shift(-horizon)`) -- and evaluate.walk_forward_splits()
    decides whether a row is safe to train on based on compute_label_end_date()'s
    output, so a backward shift lets rows from inside the test window slip
    into training. The resulting walk-forward metrics would be computed on
    leaked data, which can pass guarded_publish()'s "beats baseline" gate and
    reach live sizing. Validating here, at arg-parse time, means an invalid
    --horizon is rejected before build_dataset() is ever called -- no dataset
    construction, training, or evaluation happens on the way to the error.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"--horizon must be a strictly positive integer (got {value})")
    return value


def _positive_finite_float_validator(flag_name: str, *, timedelta_hours: bool = False):
    """argparse `type=` validator factory: finite, strictly positive floats only.

    A NaN/inf (or nonpositive) value passes argparse's bare `type=float`
    completely unvalidated, and reaches a downstream computation that
    assumes a sane positive number -- `timedelta(hours=...)` for
    --max-staleness-hours (raises ValueError/OverflowError), or
    `rescale_return_to_multiplier()`'s own `if return_clip_pct <= 0: raise
    ValueError` for --return-clip-pct, reached only AFTER training and
    walk-forward evaluation have already run. Either way the scheduled
    retrain crashes with an uncaught traceback instead of producing its
    documented REFUSED/blocked_invalid_staleness outcome and evaluation
    report (Codex review: "Validate staleness before computing the feature
    cutoff", "Validate the return clipping argument before training").

    ``timedelta_hours=True`` (used only for --max-staleness-hours) adds a
    further check that ``float`` and ``timedelta`` don't agree on: a value
    like ``1e308`` is entirely finite and positive by ``math.isfinite``'s
    own definition, but `timedelta`'s internal representation is bounded
    (~2.7e11 days), so `timedelta(hours=1e308)` still raises an uncaught
    OverflowError -- after training and evaluation have already completed,
    at `staleness_cutoff = as_of - timedelta(hours=...)` -- exactly the
    same "crashes instead of REFUSED" failure this validator otherwise
    exists to prevent (Codex review: "Bound staleness before accepting the
    CLI value"). Attempting the real construction here, rather than
    hardcoding timedelta's internal bound as a magic number, stays correct
    even if that internal limit ever changes.
    """

    def _validate(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid float value: {raw!r}") from None
        if not math.isfinite(value) or value <= 0:
            raise argparse.ArgumentTypeError(f"{flag_name} must be a finite positive number (got {value})")
        if timedelta_hours:
            try:
                timedelta(hours=value)
            except OverflowError:
                raise argparse.ArgumentTypeError(
                    f"{flag_name} is too large to represent as a duration (got {value})"
                ) from None
        return value

    return _validate


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Ticker OHLCV archive directory. Defaults to dca_ticker_paths.configured_ticker_history_dir().",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Explicit ticker list. Defaults to config/tickers.json's active universe "
        "(see --tickers-config, --all-tickers-in-archive) rather than every *.csv under --history-dir.",
    )
    parser.add_argument(
        "--tickers-config",
        type=Path,
        default=DEFAULT_TICKERS_CONFIG_PATH,
        help="config/tickers.json to read the active DCA universe from when --tickers is omitted. "
        "Ignored if --tickers or --all-tickers-in-archive is given.",
    )
    parser.add_argument(
        "--all-tickers-in-archive",
        action="store_true",
        help="Discover every *.csv under --history-dir instead of restricting to --tickers-config's "
        "active list. Off by default: archive maintenance does not delete a ticker's CSV when it is "
        "excluded from the universe, so unfiltered discovery can pool stale/excluded tickers into the "
        "shared Ridge coefficients that also drive the live universe's published adjustments. "
        "Ignored if --tickers is given.",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_CONFIG_PATH, help="config/ml_sizing_adjustment.json to (maybe) publish."
    )
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    parser.add_argument(
        "--horizon",
        type=_positive_int,
        default=5,
        help="Forward-return horizon, in trading days. Must be a strictly positive integer -- a "
        "nonpositive value would shift the label backward and leak test-window rows into training "
        "(see _positive_int's docstring).",
    )
    parser.add_argument("--n-splits", type=int, default=evaluate_module.DEFAULT_N_SPLITS)
    parser.add_argument(
        "--alpha", type=float, default=model_module.DEFAULT_ALPHA, help="Ridge regularization strength."
    )
    parser.add_argument("--min-multiplier", type=float, default=model_module.DEFAULT_MIN_MULTIPLIER)
    parser.add_argument("--max-multiplier", type=float, default=model_module.DEFAULT_MAX_MULTIPLIER)
    parser.add_argument(
        "--return-clip-pct",
        type=_positive_finite_float_validator("--return-clip-pct"),
        default=model_module.DEFAULT_RETURN_CLIP_PCT,
    )
    parser.add_argument(
        "--max-staleness-hours",
        type=_positive_finite_float_validator("--max-staleness-hours", timedelta_hours=True),
        default=publish_module.DEFAULT_MAX_STALENESS_HOURS,
    )
    parser.add_argument("--now", type=str, default=None, help="ISO8601 timestamp override, for reproducible/test runs.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Train + evaluate + write artifacts, never publish."
    )
    return parser


def _load_active_tickers(config_path: Path) -> list[str]:
    """The "active" ticker list from config/tickers.json, minus "excluded"
    and plus "whole_share" -- the live, TRADABLE DCA universe.

    This is the default training ticker set when both --tickers and
    --all-tickers-in-archive are omitted (see main()'s own comment on why
    archive-wide discovery must be explicit, not the default). Raises
    ValueError on a missing file, invalid JSON, a non-object top-level
    payload (e.g. a bare `[]`), a missing "active" key, a non-list "active"
    value, a non-list "excluded" value, or an "active" list containing
    anything that isn't a valid ticker symbol -- main() turns that into a
    REFUSED exit rather than propagating a traceback, matching this CLI's
    existing refusal style for other invalid-input states. The object/list
    checks are deliberate: payload["active"] on a non-dict payload raises
    TypeError, not one of main()'s caught exception types
    (OSError/ValueError/KeyError), and list(payload["active"]) on a
    non-list-but-iterable value (e.g. a bare string) would silently succeed
    by iterating its characters instead of raising at all (Codex review:
    "Reject non-object ticker-universe configs"). Each "active" member is
    also validated against schemas/tickers.schema.json's own symbol pattern:
    a well-formed list containing e.g. a bare int (``{"active": [1]}``)
    previously passed this check unchanged and only surfaced later, when
    main() computes `missing = sorted(set(requested_tickers) -
    set(tickers))` and then `', '.join(missing)` -- joining a list that
    still contains that int raises an uncaught TypeError instead of the
    documented REFUSED result (Codex review: "Validate every configured
    active ticker").

    "excluded" is subtracted from "active" the same way
    near_close_dca.py's own `tradable_tickers = [t for t in config.tickers
    if t not in config.excluded_tickers]` computation does for the live
    strategy -- config/tickers.json's own schema permits (does not forbid)
    a ticker appearing in BOTH lists at once (a stale entry left in "active"
    after being excluded, or vice versa), and without this filter such a
    ticker would still influence the pooled Ridge fit and receive a
    published per-ticker sizing adjustment despite the live strategy never
    actually trading it (Codex review: "Exclude non-tradable tickers from
    the default training universe"). "excluded" defaults to an empty list
    when absent -- schemas/tickers.schema.json does not require it, unlike
    "active".
    """
    payload = json.loads(config_path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError(f"{config_path} must contain a JSON object, got {type(payload).__name__}")
    active = payload["active"]
    if not isinstance(active, list):
        raise ValueError(f"{config_path}'s 'active' field must be a list, got {type(active).__name__}")
    invalid = [item for item in active if not isinstance(item, str) or not _TICKER_SYMBOL_PATTERN.match(item)]
    if invalid:
        raise ValueError(f"{config_path}'s 'active' field contains invalid ticker symbol(s): {invalid!r}")
    excluded = payload.get("excluded", [])
    if not isinstance(excluded, list):
        raise ValueError(f"{config_path}'s 'excluded' field must be a list, got {type(excluded).__name__}")
    # Validated the SAME way "active" is validated just above, not merely
    # checked for list-ness -- set(excluded) below raises an uncaught
    # TypeError for any unhashable member (e.g. `{"excluded": [{}]}` or
    # `{"excluded": [[]]}` -- both valid JSON, both pass the list check),
    # crashing a scheduled retrain with a traceback instead of the
    # documented REFUSED result, exactly the same failure mode "active"
    # itself was already fixed for above (Codex review: "Validate excluded
    # ticker members before constructing the set"). schemas/
    # tickers.schema.json's own "excluded" item type is a bare string with
    # no symbol-pattern constraint, but every real excluded entry IS a
    # ticker symbol in practice, and requiring the pattern here (not just
    # "is a string") catches a malformed entry before it silently fails to
    # match anything in "active" rather than only guarding the crash.
    invalid_excluded = [
        item for item in excluded if not isinstance(item, str) or not _TICKER_SYMBOL_PATTERN.match(item)
    ]
    if invalid_excluded:
        raise ValueError(f"{config_path}'s 'excluded' field contains invalid ticker symbol(s): {invalid_excluded!r}")
    excluded_set = set(excluded)
    # "whole_share" (see docs/whole_share_accumulation.md) is a THIRD category,
    # distinct from both "active" and "excluded" -- a whole-share ticker (e.g.
    # BYDDY) is actively purchased by the SAME graded ladder, just via a
    # quantity-based order once its accumulated contributions cross one
    # share's price, rather than a normal fractional buy. Omitting it here
    # would leave it out of every default pooled retrain and default
    # per-ticker `adjustments` publish, permanently falling back to an ML
    # factor of 1.0 in graded_buy_multiplier for a ticker the live strategy
    # genuinely trades (Codex review: "Train and publish ML sizing for
    # whole-share symbols"). Validated the SAME way "active"/"excluded" are.
    whole_share = payload.get("whole_share", [])
    if not isinstance(whole_share, list):
        raise ValueError(f"{config_path}'s 'whole_share' field must be a list, got {type(whole_share).__name__}")
    invalid_whole_share = [
        item for item in whole_share if not isinstance(item, str) or not _TICKER_SYMBOL_PATTERN.match(item)
    ]
    if invalid_whole_share:
        raise ValueError(
            f"{config_path}'s 'whole_share' field contains invalid ticker symbol(s): {invalid_whole_share!r}"
        )
    tickers = [ticker for ticker in active if ticker not in excluded_set]
    seen = set(tickers)
    for ticker in whole_share:
        if ticker not in seen and ticker not in excluded_set:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def _portable_artifact_reference(model_json_path: Path) -> str:
    """Return a reference to ``model_json_path`` that survives a change of
    checkout, host, working directory, or ``$DCA_CRON_TRACKING_ROOT``, or
    raise ``ValueError`` when no such reference exists.

    ``str(path)`` recorded either an absolute path tied to the publishing
    checkout's artifact root, or a relative one tied to that process's working
    directory. This value is persisted into the version-controlled published
    config, so the next retrain -- run from a different checkout, host or
    configured root -- could not load the incumbent, and
    :func:`_build_incumbent_scorer` silently fell back to the weaker skill-ratio
    comparison, letting a model that fails the intended direct incumbent
    comparison publish anyway (Codex review: "Store a portable incumbent model
    reference"). It also wrote machine-specific state into a tracked file, which
    ``AGENTS.md`` forbids.

    The encoding is UNAMBIGUOUS, which is what makes
    :func:`_resolve_artifact_reference` able to invert it: a RELATIVE reference
    always means "under the configured ML artifact root". A path OUTSIDE the
    root -- someone who passed ``--models-dir`` somewhere explicit -- has no
    such reference at all: an absolute path is exactly as host-tied as the
    original bug this function exists to fix, so it is REJECTED rather than
    stored (Codex review: "Keep external model references portable"), and the
    caller must not publish. This is stricter than an earlier version, which
    stored the absolute path to at least preserve where THIS host meant --
    but that config was then unusable from anywhere else, and the shared
    incumbent-comparison gate now treats a load failure as blocking, so a
    later retrain on a different host could never publish again until an
    operator repaired the path by hand.

    Returning an out-of-root path unchanged is what an even earlier version
    did, and for an external directory given RELATIVELY (``--models-dir
    ../models``) that produced a reference indistinguishable from a
    root-relative one. The resolver rebases every relative reference onto the
    root, so the next retrain looked under ``<ml artifact root>/../models``
    instead of where the model was actually saved, failed to load the
    incumbent, and silently fell back to the weaker skill-ratio comparison --
    the very failure this function exists to prevent (Codex review: "Preserve
    external relative model paths when publishing").
    """
    root = Path(stream_root("ml_forecaster"))
    resolved = Path(model_json_path)
    try:
        resolved = resolved.resolve()
    except OSError as exc:
        # Unresolvable (a broken symlink component, an unreadable parent) --
        # this path can never be made portable, so refuse to publish rather
        # than write something unusable. See the ValueError branch below for
        # why an out-of-root path is rejected rather than stored absolute.
        raise ValueError(
            f"cannot resolve model artifact path {model_json_path} for publishing: {exc}"
        ) from exc
    try:
        # as_posix(), not str(): on Windows str() yields backslash separators
        # ("models\\2026-08-17-abc\\model.json"), and a POSIX Path treats those
        # as literal filename characters rather than separators -- so a config
        # published on Windows and consumed on Linux could not locate the
        # incumbent and silently fell back to the weaker skill-ratio
        # comparison, defeating the cross-host portability this function exists
        # for (Codex review: "Serialize relative artifact references
        # portably"). Forward slashes round-trip on both platforms.
        return resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        # A --models-dir given OUTSIDE the configured ML artifact root has no
        # portable reference at all: an absolute path is tied to the
        # publishing host, and this value is persisted into the version-
        # controlled published config where AGENTS.md forbids machine-
        # specific state. A prior version stored it absolute anyway, so the
        # NEXT retrain -- on another checkout/host -- could not load the
        # incumbent and _build_incumbent_scorer silently fell back to the
        # weaker skill-ratio comparison, letting a model that fails the
        # intended direct incumbent comparison publish (Codex review: "Keep
        # external model references portable"). Refusing to publish at all
        # is the fail-closed direction: the operator gets an actionable error
        # naming the two real fixes -- move --models-dir under the shared
        # root, or publish that config manually -- rather than a config that
        # silently degrades on every OTHER host.
        raise ValueError(
            f"model artifact {resolved} lies outside the ML artifact root {root.resolve()}; "
            "a config referencing it cannot be loaded from another checkout/host. Pass "
            "--models-dir under the configured artifact root, or move the model there before "
            "publishing."
        ) from exc


def _resolve_artifact_reference(model_artifact_path: str) -> Path:
    """Inverse of :func:`_portable_artifact_reference`.

    A relative reference is resolved against the CURRENT ML artifact root, so a
    model published from one checkout/host/root is loadable from another. An
    absolute one is used as given -- that covers both a deliberately external
    ``--models-dir`` and every legacy config written before this was stored
    portably.

    Rebasing relative references is why :func:`_portable_artifact_reference`
    must store an out-of-root path absolutely: in a reference written by that
    function, a relative path can only ever mean root-relative.

    A config published by an EARLIER version carries no such guarantee. The old
    code passed ``str(model_json_path)`` through unchanged, so an explicit
    relative ``--models-dir`` recorded a path relative to that retrain's WORKING
    DIRECTORY. Rebasing it unconditionally looks under the ML artifact root,
    fails to find an incumbent that is really still there, and drops
    :func:`_build_incumbent_scorer` to the weaker skill-ratio comparison --
    exactly the silent downgrade this pair of functions exists to prevent
    (Codex review: "Preserve pre-change relative artifact references"). So the
    root-relative reading is TRIED FIRST and the legacy working-directory
    reading is used only as a fallback, and only when it actually resolves to a
    file. Existence is the disambiguator: the new contract wins wherever both
    could apply, and no legacy config is silently downgraded while its model is
    still loadable.

    When neither exists the root-relative path is returned, so the caller's
    failure refers to where the artifact was supposed to be rather than to a
    stale working-directory guess.
    """
    # A stored reference always uses forward slashes (see
    # _portable_artifact_reference), but a legacy config written on Windows by
    # the previous version can carry backslashes. PurePosixPath-style parsing on
    # Linux would treat those as one long filename, so normalize before
    # resolving -- on Windows both separators already parse correctly.
    path = Path(model_artifact_path.replace("\\", "/") if os.sep == "/" else model_artifact_path)
    if path.is_absolute():
        return path
    rooted = Path(stream_root("ml_forecaster")) / path
    if rooted.exists():
        return rooted
    if path.exists():
        return path.resolve()
    return rooted


def _build_incumbent_scorer(dip_dataset, candidate_model: model_module.TrainedModel):
    """A `score_incumbent_model` callback for publish.guarded_publish() (see
    that function's own docstring): given the currently-published model's
    recorded `model_artifact_path`, loads it (plain JSON, no pickle -- see
    model.TrainedModel's own docstring on why that's safe), and compares it
    with an aligned candidate against THIS run's own `dip_dataset`
    (evaluate.score_incumbent_on_folds()).

    `candidate_model` supplies the prospective final model's hyperparameters.
    score_incumbent_on_folds() fits an aligned comparison model on the early
    portion of post-incumbent history and scores both models on a later
    holdout.  It deliberately does not score this final model directly: the
    final model has already seen the complete current dataset, which would
    make its holdout score in-sample and unfairly favor publication.

    Returns None on any load/parse failure, or if the resulting score isn't
    usable -- publish.blocking_reasons() falls back to its existing
    window-match/skill-ratio comparisons when this returns None, so a
    missing or corrupted incumbent artifact never blocks a retrain outright
    (Codex review: "Compare the incumbent on the current evaluation folds").
    """

    def _score(model_artifact_path: str) -> tuple[float, float] | None:
        try:
            incumbent_model = model_module.TrainedModel.load(
                _resolve_artifact_reference(model_artifact_path)
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        return evaluate_module.score_incumbent_on_folds(incumbent_model, candidate_model, dip_dataset)

    return _score


def _resolve_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# ml-forecaster eval report -- {report.get('model_version', '?')}",
        "",
        f"Status: **{report.get('status', '?')}**",
        "",
        f"- History dir: `{report.get('history_dir', '?')}`",
        f"- Tickers: {len(report.get('tickers', []))}",
        f"- Dataset: {report.get('dataset', {}).get('n_rows_dip_days', '?')} dip-day rows "
        f"(of {report.get('dataset', {}).get('n_rows_all', '?')} total feature rows) across "
        f"{report.get('dataset', {}).get('n_tickers', '?')} tickers",
        f"- Walk-forward folds: {report.get('evaluation', {}).get('n_splits', '?')}, "
        f"alpha={report.get('evaluation', {}).get('alpha', '?')}",
        "",
        "## Overall (mean across folds)",
        "",
        "| metric | model | baseline: predict-zero | baseline: predict-trailing-mean |",
        "|---|---|---|---|",
    ]
    overall = report.get("evaluation", {}).get("overall", {})
    for label, model_key, zero_key, mean_key in (
        ("MAE", "model_mae", "baseline_zero_mae", "baseline_mean_mae"),
        ("RMSE", "model_rmse", "baseline_zero_rmse", "baseline_mean_rmse"),
        ("Hit rate", "model_hit_rate", "baseline_zero_hit_rate", "baseline_mean_hit_rate"),
    ):
        model_value = overall.get(model_key)
        zero_value = overall.get(zero_key)
        mean_value = overall.get(mean_key)
        lines.append(
            f"| {label} | {model_value:.4f} | {zero_value:.4f} | {mean_value:.4f} |"
            if all(isinstance(v, (int, float)) for v in (model_value, zero_value, mean_value))
            else f"| {label} | n/a | n/a | n/a |"
        )
    lines.append("")

    publish_result = report.get("publish_result")
    if publish_result:
        lines.append("## Publish decision")
        lines.append("")
        lines.append(f"Status: `{publish_result.get('status')}`")
        for reason in publish_result.get("reasons", []):
            lines.append(f"- {reason}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _write_eval_report(run_dir: Path, report: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "eval_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "eval_report.md").write_text(_render_markdown(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # --min-multiplier/--max-multiplier are plain `type=float`, unlike
    # --return-clip-pct/--max-staleness-hours (both use
    # _positive_finite_float_validator) -- a NaN/inf or inverted pair
    # (min > max) can't be rejected by a single-argument argparse `type=`
    # validator, so it's checked here instead, reusing publish.py's own
    # schema-floor/ceil + ordering check rather than duplicating it. Checked
    # BEFORE training/evaluation, for --dry-run as much as for a live
    # publish: rescale_return_to_multiplier() runs unconditionally (dry-run
    # never calls guarded_publish(), which is the only place these bounds
    # were previously validated), so an invalid pair would otherwise reach
    # it unchecked and write inverted or non-finite proposed adjustments
    # into the dry-run eval report while still exiting 0 (Codex review:
    # "Validate multiplier bounds before dry-run scoring").
    bound_problems = publish_module._validate_bounds(args.min_multiplier, args.max_multiplier)
    if bound_problems:
        for problem in bound_problems:
            print(f"REFUSED: {problem}", file=sys.stderr)
        return 1
    try:
        now = _resolve_now(args.now)
    except ValueError as exc:
        # datetime.fromisoformat() raises ValueError for anything that
        # isn't a real ISO 8601 timestamp (e.g. "not-a-date"), and this call
        # sat outside every refusal boundary -- an operator or scheduled
        # wrapper supplying a malformed --now crashed the whole retrain with
        # a traceback before it ever produced its documented REFUSED result
        # and evaluation report, instead of failing cleanly like every other
        # invalid-input case in this CLI (Codex review: "Validate --now
        # before starting the retrain").
        print(f"REFUSED: --now {args.now!r} is not a valid ISO 8601 timestamp: {exc}", file=sys.stderr)
        return 1
    history_dir = args.history_dir or configured_ticker_history_dir()

    if args.tickers is not None:
        requested_tickers: list[str] | None = args.tickers
    elif args.all_tickers_in_archive:
        requested_tickers = None
    else:
        # Default: the configured active universe, NOT every CSV in the
        # archive -- see the module docstring and --all-tickers-in-archive's
        # own help text for why unfiltered discovery is opt-in only.
        try:
            requested_tickers = _load_active_tickers(args.tickers_config)
        except (OSError, ValueError, KeyError) as exc:
            print(
                f"REFUSED: could not read active ticker universe from {args.tickers_config}: {exc}",
                file=sys.stderr,
            )
            return 1

    tickers = discover_available_tickers(history_dir, requested_tickers)
    if requested_tickers is not None:
        missing = sorted(set(requested_tickers) - set(tickers))
        if missing:
            print(
                f"NOTE: {len(missing)} requested ticker(s) have no archived CSV under {history_dir}, "
                f"skipping: {', '.join(missing)}",
                file=sys.stderr,
            )
    if not tickers:
        print(f"REFUSED: no ticker history CSVs found under {history_dir}", file=sys.stderr)
        return 1

    # Naive UTC, matching the "Date" column's own dtype (see features.py's
    # load_history_csv()) -- comparing a tz-aware Timestamp against a naive
    # datetime64 column raises. Excludes every archived row dated after
    # `now` from feature/target computation entirely: --now can be set
    # earlier than the archive tail for reproducible/test replay runs, and
    # without this a "historical" run would silently train, evaluate, and
    # score against rows (and rolling features/targets derived FROM them)
    # that had not happened yet as of the replayed "now" -- lookahead-
    # contaminated metrics and adjustments in an artifact that still claims
    # the earlier run_at (Codex review: "Exclude observations after the
    # effective run time").
    as_of = now.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        dataset = build_dataset(history_dir, tickers, horizon=args.horizon, as_of=as_of)
    except (OSError, ValueError) as exc:
        # load_history_csv() (reached per-ticker inside build_dataset())
        # raises ValueError for a file-level archive problem -- e.g. a CSV
        # missing a required OHLCV column, or one whose Date column doesn't
        # parse to datetimes at all -- and this call sat outside every
        # refusal boundary, unlike the walk-forward evaluation call just
        # below (which already wraps evaluate_walk_forward() in an
        # identical try/except). A single malformed archived CSV crashed
        # the whole weekly retrain with a traceback before it ever created
        # a run directory or evaluation report, instead of producing the
        # documented REFUSED result while leaving the live publication
        # untouched (Codex review: "Refuse malformed archive files
        # cleanly").
        print(f"REFUSED: could not build the training dataset from {history_dir}: {exc}", file=sys.stderr)
        return 1
    dip_dataset = cast(pd.DataFrame, dataset[dataset["dip_day"]].reset_index(drop=True)) if not dataset.empty else dataset

    required_rows = max(MINIMUM_DIP_DAY_ROWS, args.n_splits + 1)
    if len(dip_dataset) < required_rows:
        print(
            f"REFUSED: only {len(dip_dataset)} usable dip-day rows across {len(tickers)} tickers -- "
            f"need at least {required_rows} for a meaningful walk-forward evaluation",
            file=sys.stderr,
        )
        return 1

    # A short random suffix (not just the date) guarantees run_dir/model_version
    # never collide across two invocations on the same UTC date -- a retry
    # after an earlier failed/rejected publish, a later manual re-run, or a
    # --dry-run invocation would otherwise all write to the identical
    # models/<date>/ path, silently overwriting the artifact for a model that
    # was successfully published earlier that same day even though the live
    # config/ml_sizing_adjustment.json still names that now-overwritten
    # model_version (Codex review: "Preserve same-day model artifacts across
    # retraining attempts"). A timestamp alone is not enough: --now can be
    # passed explicitly (e.g. by tests), and two runs could share it exactly.
    run_id = uuid.uuid4().hex[:8]
    model_version = f"{now.date().isoformat()}-{run_id}-ridge-v1"
    try:
        evaluation = evaluate_module.evaluate_walk_forward(dip_dataset, n_splits=args.n_splits, alpha=args.alpha)
    except ValueError as exc:
        print(f"REFUSED: walk-forward evaluation could not run: {exc}", file=sys.stderr)
        return 1

    final_model = model_module.fit(
        dip_dataset,
        alpha=args.alpha,
        model_version=model_version,
        return_clip_pct=args.return_clip_pct,
        trained_at=now.isoformat(),
        horizon_trading_days=args.horizon,
    )

    # Published adjustments are read off each ticker's MOST RECENT available
    # row in the FULL feature frame (not restricted to dip days, and NOT
    # `dataset` above) -- representing the model's current read of that
    # ticker's momentum/volatility/drawdown state. The published multiplier
    # is looked up whenever a dip NEXT occurs (near_close_dca.
    # graded_buy_multiplier()), not only on days that were themselves dips,
    # so "today's" (most recent archived) feature row is the right snapshot
    # to score. Deliberately built from build_feature_frame(), NOT `dataset`:
    # `dataset` is build_dataset()'s TARGET-TRIMMED frame, which has already
    # dropped each ticker's most recent `horizon` trading days because their
    # forward-return target isn't computable yet -- tail()-ing THAT frame
    # would score a stale row from roughly `horizon` trading days ago, not
    # the actual latest archived row (see features.build_feature_frame()'s
    # own docstring). Training itself stays restricted to historical dip
    # days, matching the target's own conditioning (see model.py's
    # docstring) -- this is a deliberate, documented v1 simplification: a
    # future iteration could instead publish only for tickers currently
    # mid-dip, or refresh adjustments intraday.
    feature_frame = build_feature_frame(history_dir, tickers, as_of=as_of)
    latest_rows = feature_frame.sort_values("date", kind="stable").groupby("ticker", as_index=False).tail(1)

    # A ticker whose archive refresh has stalled (or whose whole archive is
    # old) still has SOME latest row -- silently scoring it would let
    # guarded_publish() stamp a fresh generated_at over a months-old
    # snapshot, and the live loader only checks generated_at, not the
    # feature date it was computed from (Codex review: "Reject stale feature
    # snapshots before publishing"). Refuse any row older than the same
    # staleness budget the published config itself is held to.
    staleness_cutoff = as_of - timedelta(hours=args.max_staleness_hours)
    stale_mask = latest_rows["date"] < staleness_cutoff
    if stale_mask.any():
        stale_tickers = sorted(latest_rows.loc[stale_mask, "ticker"].astype(str).str.upper())
        print(
            f"WARNING: excluding stale feature snapshots (older than {args.max_staleness_hours}h) "
            f"for: {', '.join(stale_tickers)}",
            file=sys.stderr,
        )
        latest_rows = cast(pd.DataFrame, latest_rows.loc[~stale_mask].reset_index(drop=True))

    predicted_returns = final_model.predict_return_pct(latest_rows)
    # A non-finite prediction (an overflowing design-matrix computation from
    # an extreme feature value) is filtered out here, before it ever reaches
    # rescale_return_to_multiplier -- that function's own min/max clipping
    # would otherwise map a NaN input to max_multiplier and an infinite one
    # to max_multiplier/min_multiplier, producing an apparently-valid,
    # schema-passing multiplier from what is really a broken computation
    # (Codex review: "Reject non-finite forecasts before clipping"). Mirrors
    # the stale-snapshot exclusion just above: skip the affected ticker's
    # adjustment, warn, and keep going for the rest.
    adjustments: dict[str, float] = {}
    non_finite_tickers = []
    for ticker, predicted_return in zip(latest_rows["ticker"].to_numpy(), predicted_returns, strict=True):
        predicted_return = float(predicted_return)
        if not math.isfinite(predicted_return):
            non_finite_tickers.append(str(ticker).upper())
            continue
        adjustments[str(ticker).upper()] = model_module.rescale_return_to_multiplier(
            predicted_return,
            min_multiplier=args.min_multiplier,
            max_multiplier=args.max_multiplier,
            return_clip_pct=args.return_clip_pct,
        )
    if non_finite_tickers:
        print(
            f"WARNING: excluding non-finite predicted returns for: {', '.join(sorted(non_finite_tickers))}",
            file=sys.stderr,
        )

    run_dir = args.models_dir / f"{now.date().isoformat()}-{run_id}"
    model_json_path = run_dir / "model.json"
    final_model.save(model_json_path)

    report: dict[str, Any] = {
        "model_version": model_version,
        "run_at": now.isoformat(),
        "history_dir": str(history_dir),
        "horizon_trading_days": args.horizon,
        "tickers": tickers,
        "dataset": {
            "n_rows_all": len(dataset),
            "n_rows_dip_days": len(dip_dataset),
            "n_tickers": dataset["ticker"].nunique() if not dataset.empty else 0,
        },
        "evaluation": evaluation,
        "adjustments": adjustments,
        "status": "evaluated",
    }

    if not adjustments:
        # Every ticker's latest snapshot was excluded by the staleness
        # filter above (or none survived rolling-window warmup) -- an empty
        # `adjustments` mapping still passes guarded_publish()'s own gate
        # (its regression check only looks at walk-forward MAE, which is
        # unrelated to per-ticker snapshot staleness), so it would happily
        # publish a fresh, active config with NO per-ticker overrides at
        # all -- silently reverting every live sizing adjustment to its
        # implicit 1.0 no-op instead of refusing outright (Codex review:
        # "Refuse publishing when every feature snapshot is stale").
        print(
            "REFUSED: no fresh feature snapshot survived staleness filtering -- refusing to publish an "
            "empty adjustments set that would silently remove every live sizing adjustment",
            file=sys.stderr,
        )
        report["status"] = "blocked_no_fresh_snapshot"
        _write_eval_report(run_dir, report)
        if args.dry_run:
            print(f"DRY_RUN model={model_version} mae={evaluation['overall'].get('model_mae'):.6f}")
            return 0
        return 1

    if args.dry_run:
        report["status"] = "dry_run_not_published"
        _write_eval_report(run_dir, report)
        print(f"DRY_RUN model={model_version} mae={evaluation['overall'].get('model_mae'):.6f}")
        return 0

    # generated_at is deliberately the REAL wall clock at publish time, not
    # `now` (which can be an earlier --now replay timestamp, supported for
    # reproducible/test runs). The core loader's own staleness check always
    # judges the LIVE config against the real wall clock, so persisting a
    # replay's `now` here would either get immediately rejected as stale (a
    # historical replay) or as a negative-age "future" publish (a
    # forward-dated one) -- either way, a successful non-dry-run replay
    # could silently overwrite a working live publication with one the
    # loader refuses to ever apply (Codex review: "Prevent replay
    # timestamps from replacing the live publish"). The publish action
    # itself is genuinely happening right now regardless of which data
    # snapshot (`as_of`/`now`) it was trained and scored against.
    real_now = datetime.now(timezone.utc)
    # A --now set AHEAD of the real wall clock -- clock skew, an operator
    # mistake, or a replay/test timestamp accidentally reaching a live
    # invocation -- must be refused outright for a LIVE (non-dry-run)
    # publish, never merely bounded by the same staleness budget a PAST
    # drift is. `as_of` (derived from `now`, computed far above this point)
    # is what gates which archived rows are even considered "available" for
    # feature/target computation and for the model-scoring "latest row"
    # snapshot below -- a forward-dated `now` can therefore admit archived
    # observations dated AFTER the real publication moment (e.g. a close
    # the archive already recorded slightly ahead of true wall time due to
    # timezone/DST handling elsewhere) as if they were legitimately known
    # at publish time. Because generated_at is stamped from the REAL
    # real_now (see the comment above, not `now`), the resulting config
    # would then be consumed by the live loader as a perfectly fresh
    # adjustment, with no trace that its own underlying data cutoff was
    # never actually reachable at that real moment (Codex review: "Refuse
    # forward-dated live replays"). The prior check merely bounded the
    # ABSOLUTE drift, which treats a future `now` the same as a past one --
    # but a PAST replay's data cutoff can never see anything not genuinely
    # already available, so only the forward-dated direction carries this
    # lookahead risk and needs its own unconditional refusal.
    if now > real_now:
        print(
            f"REFUSED: --now ({now.isoformat()}) is ahead of the real wall clock "
            f"({real_now.isoformat()}) -- refusing to publish live adjustments computed "
            "from a forward-dated data cutoff, which could admit archived observations "
            "not actually available at the real publication time",
            file=sys.stderr,
        )
        report["status"] = "blocked_replay_not_current"
        _write_eval_report(run_dir, report)
        return 1

    # The row-level staleness filter above validated latest_rows against
    # `as_of` (this run's --now), not against real_now -- correct for a
    # --dry-run replay (which legitimately wants to simulate "was the data
    # stale AS OF that historical point"), but not sufficient to gate a
    # LIVE publish: generated_at is now correctly real_now (see the comment
    # above), so the live loader would treat this publish as fresh even
    # though the adjustments it carries were computed from a snapshot that
    # is only "fresh" relative to a --now far from when the publish is
    # actually happening. A historical replay's own data could otherwise
    # be published live as if it were current (Codex review: "Block
    # historical replays from publishing live adjustments"). Refuse
    # outright rather than let guarded_publish() run at all whenever --now
    # drifts from the real wall clock by more than the same staleness
    # budget the row-level filter already uses. `now` is guaranteed <=
    # real_now here (the forward-dated check above already returned
    # otherwise), so this is always a non-negative PAST drift, no abs()
    # needed.
    replay_drift_hours = (real_now - now).total_seconds() / 3600.0
    if replay_drift_hours > args.max_staleness_hours:
        print(
            f"REFUSED: --now ({now.isoformat()}) differs from the real wall clock by "
            f"{replay_drift_hours:.1f}h, beyond --max-staleness-hours ({args.max_staleness_hours}h) -- "
            "refusing to publish a replay/test run's adjustments as if they were live",
            file=sys.stderr,
        )
        report["status"] = "blocked_replay_not_current"
        _write_eval_report(run_dir, report)
        return 1

    # The row-level staleness filter earlier validated latest_rows against
    # `as_of` (this run's --now), not against real_now -- correct for the
    # --dry-run/eval report above, which legitimately wants "was this stale
    # AS OF that historical point," but not sufficient to gate what actually
    # gets PUBLISHED live: a replay whose --now itself passed the drift
    # check above, and whose latest_rows also passed the as_of-relative
    # filter, can still carry a feature snapshot that is stale relative to
    # the REAL wall clock this publish is happening at -- e.g. with a
    # 240h/10-day budget, a --now nine days in the past scoring a snapshot
    # that was ALSO nine days stale as of that --now is, relative to
    # real_now, eighteen days old: comfortably under both individual
    # 10-day checks while still genuinely stale live data (Codex review:
    # "Check replay snapshots against the publication clock"). Re-validate
    # latest_rows' actual feature dates against real_now, immediately
    # before the live-only guarded_publish() call below.
    live_staleness_cutoff = (real_now - timedelta(hours=args.max_staleness_hours)).replace(tzinfo=None)
    live_stale_mask = latest_rows["date"] < live_staleness_cutoff
    if live_stale_mask.any():
        live_stale_tickers = set(latest_rows.loc[live_stale_mask, "ticker"].astype(str).str.upper())
        print(
            "WARNING: excluding feature snapshots stale relative to the real publish clock "
            f"(older than {args.max_staleness_hours}h) for: {', '.join(sorted(live_stale_tickers))}",
            file=sys.stderr,
        )
        adjustments = {ticker: value for ticker, value in adjustments.items() if ticker not in live_stale_tickers}
        report["adjustments"] = adjustments

    if not adjustments:
        print(
            "REFUSED: no fresh feature snapshot survived staleness filtering against the real "
            "publish clock -- refusing to publish an empty adjustments set that would silently "
            "remove every live sizing adjustment",
            file=sys.stderr,
        )
        report["status"] = "blocked_no_fresh_snapshot"
        _write_eval_report(run_dir, report)
        return 1

    # The live loader (dca_config.load_ml_sizing_adjustment) measures
    # staleness ONLY from this file's own `generated_at`, never from the
    # underlying feature snapshot's own date -- a snapshot that was already,
    # say, 239h old at publish time (still under the 240h admission filter
    # above) would otherwise get ANOTHER full max_staleness_hours budget
    # once published, so the market data actually driving a still-"fresh"
    # adjustment could influence live orders for close to DOUBLE the
    # configured budget before the loader finally treats the file as stale
    # (Codex review: "Age published adjustments from their feature
    # snapshots"). Shrink the budget actually WRITTEN into the payload by
    # however stale the least-fresh SURVIVING snapshot already was at
    # real_now, so the live loader's own window reflects each snapshot's
    # REMAINING freshness rather than a fresh full window regardless of how
    # old the data already was. Always strictly positive here: every
    # surviving ticker already passed live_stale_mask above (age strictly
    # less than max_staleness_hours).
    surviving_dates = latest_rows.loc[
        latest_rows["ticker"].astype(str).str.upper().isin(adjustments), "date"
    ]
    oldest_surviving_snapshot_age_hours = float(
        ((real_now.replace(tzinfo=None) - surviving_dates).dt.total_seconds() / 3600.0).max()
    )
    effective_max_staleness_hours = args.max_staleness_hours - oldest_surviving_snapshot_age_hours

    result = publish_module.guarded_publish(
        adjustments=adjustments,
        eval_summary=evaluation,
        model_version=model_version,
        config_path=args.output,
        min_multiplier=args.min_multiplier,
        max_multiplier=args.max_multiplier,
        max_staleness_hours=effective_max_staleness_hours,
        horizon_trading_days=args.horizon,
        # The ticker population evaluate_walk_forward() actually scored
        # (dip_dataset's own tickers, not the broader --tickers request some
        # of which may have had zero usable dip-day rows) -- lets the NEXT
        # retrain's guarded_publish() call refuse to compare against this
        # run's MAE if ITS evaluated universe differs (Codex review:
        # "Compare models on the same evaluation sample").
        evaluation_universe=sorted(dip_dataset["ticker"].astype(str).str.upper().unique()),
        generated_at=real_now,
        model_artifact_path=_portable_artifact_reference(model_json_path),
        score_incumbent_model=_build_incumbent_scorer(dip_dataset, final_model),
    )
    report["status"] = result["status"]
    report["publish_result"] = result
    _write_eval_report(run_dir, report)

    print(f"{result['status'].upper()} model={model_version} mae={evaluation['overall'].get('model_mae'):.6f}")
    return 0 if result["status"] == "published" else 1


if __name__ == "__main__":
    raise SystemExit(main())
