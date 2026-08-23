"""CLI: one live-day cycle -- the loop that repeats forever after the replay.

Run this once per day after the archive has been refreshed with the newest
closed trading day (the repo's external ticker-update pipeline owns that;
scheduling THIS command -- cron, a Routine, launchd -- is deliberately the
operator's choice and out of scope here). Each invocation:

1. rebuilds the panel from the archive (re-applying the run's recorded
   universe-selection rule, so newly-listed tickers join exactly as the
   original selection would have admitted them),
2. resumes the run's checkpoint against that panel and re-binds forecasts
   whose target date has just become known,
3. executes every not-yet-processed trading day (normally exactly one):
   score matured forecasts, run the encoder diagnostic, online-update,
   predict the next day, checkpoint,
4. writes ``daily/<date>_summary.json`` and -- the headline output --
   ``daily/<date>_next_day_forecasts.csv``: tomorrow's predicted open, high,
   low, and close (with p10-p90 bands, in adjusted dollars) for every
   eligible ticker,
5. prints a one-glance human digest.

Idempotent: rerunning after a day is already processed exits 0 with
``NOOP: <date> already processed`` and writes nothing.

Exit codes: 0 processed-or-NOOP, 1 REFUSED, 2 runtime failure (checkpoint
retained), matching run_walkforward.py.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
# Index 1, not 0 -- see run_walkforward.py's identical bootstrap comment.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402

import features  # noqa: E402 # type: ignore
import metrics as metrics_module  # noqa: E402
from baseline_model import TrainingDataChanged  # noqa: E402
from data import COMPONENTS  # noqa: E402
from harness import (  # noqa: E402
    UNRESOLVED_KEY,
    HarnessConfig,
    HarnessError,
    WalkForwardHarness,
    _acquire_run_lock,
    _release_run_lock,
)
from ledger import Ledger, LedgerError  # noqa: E402
from model_interface import QUANTILE_KEYS  # noqa: E402
from run_walkforward import (  # noqa: E402
    Refused,
    _install_early_signal_handlers,
    _install_signal_handlers,
    _model_registry,
    _parse_now,
    _verify_resume_not_before_cursor,
    refresh_run_meta_universe,
)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write via a same-directory temp file + rename, never a direct write.

    ``<date>_summary.json``'s mere EXISTENCE is what the NOOP path above
    treats as "already recovered" (see the ``summary_path.exists()`` check).
    A kill between opening the file and finishing the write leaves a zero-
    length or truncated JSON at that path; the next invocation's existence
    check would then accept it as complete and never repair it (Codex
    review: "Validate daily summaries before treating them as recovered").
    ``Path.replace`` is an atomic rename on the same filesystem, so readers
    only ever see the old complete file or the new complete file.
    """
    tmp_path = path.parent / f"{path.name}.tmp"
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _daily_artifacts_are_complete(summary_path: Path) -> bool:
    """``summary_path.exists()`` alone is not proof the artifact SET is
    complete: the summary and the next-day forecasts CSV are two
    SEPARATELY written files, and a power loss between the two (or a
    later invocation that regenerates one but not the other) can leave a
    valid summary referencing a ``next_day_forecasts_csv`` path that is
    missing or was never actually written. Every later same-day NOOP used
    to accept a bare ``summary_path.exists()`` as fully recovered forever,
    so the headline forecast CSV was never regenerated even though the
    checkpoint still holds everything needed to rebuild it (Codex review:
    "Verify the forecast CSV before returning NOOP"). A null
    ``next_day_forecasts_csv`` is legitimate (no ticker had a pending
    forecast that day, see _write_next_day_forecasts()'s own empty-rows
    return); only a NON-null path that fails to exist, or a summary that
    fails to parse at all, means recovery is still needed.
    """
    if not summary_path.exists():
        return False
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    forecasts_csv = summary.get("next_day_forecasts_csv")
    return forecasts_csv is None or Path(forecasts_csv).exists()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily_update.py", description="Wondrous Octopus live daily cycle."
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="a run directory holding a checkpoint")
    parser.add_argument("--history-dir", type=Path, default=None)
    parser.add_argument("--now", default=None, help="as-of override: rows on/after this day are unseen")
    parser.add_argument("--dry-run", action="store_true", help="report what would run; write nothing")
    return parser


def _load_stored_config(run_dir: Path) -> HarnessConfig:
    state_path = run_dir / "checkpoint" / "harness_state.json"
    if not state_path.exists():
        fallback = run_dir / "checkpoint.prev" / "harness_state.json"
        state_path = fallback if fallback.exists() else state_path
    if not state_path.exists():
        raise Refused(f"no checkpoint under {run_dir}; run run_walkforward.py first")
    stored = json.loads(state_path.read_text(encoding="utf-8"))["config"]
    stored["models"] = tuple(stored["models"])
    config = HarnessConfig(**stored)
    config.end = None  # the live cycle always runs to the newest closed day
    # Per-invocation circuit-breaker budgets belong to the replay invocation
    # that set them, not to a daily catch-up of a handful of days.
    config.max_steps_per_run = None
    config.max_hours_per_run = None
    return config


def _load_run_meta(run_dir: Path) -> dict:
    """The run's frozen ticker selection + original archive location.

    Required to safely rebuild the SAME universe on every daily
    invocation. If the process died between fresh()'s checkpoint and
    _write_run_meta() running, run_meta.json can be absent even though a
    valid-looking checkpoint exists; silently falling back to "the whole
    archive" for selection or "the machine's configured default archive"
    for history_dir in that case can expand the vocabulary, refit GBM, and
    ledger forecasts for a DIFFERENT universe or dataset than the run was
    ever validated against -- refuse instead (Codex review: "Refuse daily
    updates without frozen run metadata").
    """
    meta_path = run_dir / "run_meta.json"
    if not meta_path.exists():
        raise Refused(
            f"no run_meta.json under {run_dir}; the recorded ticker selection and archive location are "
            "required to safely continue this run (rerun run_walkforward.py, or restore run_meta.json "
            "from a backup)"
        )
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _load_selection(meta: dict) -> dict:
    return meta.get("selection", {"tickers": None, "max_tickers": None})


def _load_history_dir(meta: dict) -> Path | None:
    """The archive directory the run was ORIGINALLY built from, if recorded."""
    recorded = meta.get("history_dir")
    return Path(recorded) if recorded else None


def _cursor_from_checkpoint_state(state: dict, panel, config: HarnessConfig) -> int:
    """Mirror WalkForwardHarness.resume()'s cursor anchor, from checkpoint
    state alone -- no panel-eligibility replay, no model loading. Shared by
    the --dry-run advisory path and _dispatch()'s same-day-NOOP short
    circuit below, so the two can't silently disagree on what "caught up"
    means.
    """
    last_processed = state.get("last_processed_date")
    # Mirror WalkForwardHarness.resume()'s exact fallback: if no step has
    # EVER completed (e.g. --max-hours 0 tripped immediately, or a signal
    # arrived during the initial fit), last_processed_date is null. Falling
    # back to cursor 0 here reported every pre-start archive date
    # (potentially decades of them) as "pending" instead of anchoring at
    # config.start like the real resume path does (Codex review: "Anchor
    # first-step daily dry runs at the configured start").
    return (
        int(np.searchsorted(panel.calendar, np.datetime64(last_processed), side="right"))
        if last_processed
        else int(np.searchsorted(panel.calendar, np.datetime64(config.start)))
    )


def _count_pending_rebinds(state: dict, panel) -> int:
    """How many unresolved/overdue forecasts the rebuilt panel can now resolve.

    Read-only mirror of WalkForwardHarness.rebind_unresolved()'s own
    matching logic. Both callers only invoke this when the panel already
    has NO pending calendar dates left -- exactly the condition
    rebind_unresolved() itself uses to decide whether to score its
    recoveries immediately rather than requeue them for a future step().
    rebind_unresolved() fires even in that case -- a lagging ticker gaining
    its next row on a date already present in the global calendar -- and
    when it does, it scores immediately, appends to the ledger, and
    checkpoints; the dry-run branch must disclose that work rather than
    reporting a plain NOOP (Codex review: "Include rebound scoring in
    daily dry-run plans").
    """
    outstanding = state.get("outstanding", {})
    unresolved = outstanding.get(UNRESOLVED_KEY, [])
    count = 0
    for entry in unresolved:
        series = panel.series.get(entry["ticker"])
        if series is None:
            continue
        # Calendar-day comparison, mirroring rebind_unresolved()'s own fix
        # (Codex review: "Rebind live forecasts by calendar day") -- this
        # function must match its matching logic exactly, or the dry-run
        # disclosure would disagree with what the real invocation does.
        positions = np.flatnonzero(series.dates.astype("datetime64[D]") == np.datetime64(entry["origin_date"]))
        if not len(positions):
            # The ticker itself is present, but THIS entry's own origin row
            # is gone -- an archive correction deleted or shifted it.
            # rebind_unresolved() now treats this as a mutation of
            # self.outstanding: it unconditionally drops the stale entry
            # (Codex review: "Drop unresolved forecasts whose origin row
            # was deleted") and may then issue a fresh terminal-row
            # replacement forecast for the ticker (Codex review: "Treat
            # newly admitted terminal rows as pending work"). Either way
            # real persisted state changes, so the NOOP short-circuit must
            # not bypass it by reporting no pending work here (Codex
            # review: "Count deleted-origin forecasts as pending work").
            count += 1
            continue
        label_end = series.label_end_dates[int(positions[0])]
        if not np.isnat(label_end):
            count += 1
    # DATED buckets step()'s own missing-ticker branch re-queues VERBATIM
    # under their unchanged key (the target date was already known --
    # only the ticker's row was temporarily unavailable that day). With no
    # calendar step left, nothing else will ever sweep them again, so a
    # ticker's data being restored mid-day -- without the whole-market
    # calendar itself advancing -- left this already-overdue forecast
    # neither scored nor counted here, understating what a real invocation
    # now does after the matching rebind_unresolved() fix (Codex review:
    # "Process restored dated forecasts before NOOP").
    for key, bucket in outstanding.items():
        if not key or key == UNRESOLVED_KEY:
            continue
        for entry in bucket:
            if entry["ticker"] in panel.series:
                count += 1
    # Mirrors rebind_unresolved()'s own newly-admitted-terminal-row check
    # (Codex review: "Treat newly admitted terminal rows as pending work")
    # -- a ticker that just joined the CURRENT panel's terminal row eligible
    # for its first-ever forecast (a backfilled IPO, a restored CSV, a
    # capped-universe ranking swap) has NO outstanding entry at all yet, so
    # neither loop above ever sees it. Without this, the NOOP short-circuit
    # fired before WalkForwardHarness.resume() + rebind_unresolved() ever
    # ran, permanently skipping that ticker's only chance at a forecast for
    # its terminal row -- the CSV export omits it forever, since once the
    # ticker's actual next row arrives that missed forecast can never be
    # issued retroactively.
    if len(panel.calendar):
        import pandas as pd

        already_forecast = {entry["ticker"] for entry in unresolved}
        terminal_date64 = panel.calendar[-1]
        terminal_date = pd.Timestamp(terminal_date64)
        candidates: dict[str, int] = dict(panel.eligible_on(terminal_date))
        # eligible_on(the market-wide final date) only surfaces a ticker
        # whose OWN terminal row lands on THAT exact date. A ticker whose
        # restored/newly warmed-up CSV is one session behind the rest of
        # the archive has its true final row on an EARLIER date, so it
        # never appears there even though that row is itself eligible and
        # finite. Must mirror WalkForwardHarness.rebind_unresolved()'s
        # matching fix exactly (Codex review: "Forecast newly eligible
        # tickers from their own terminal row"), or this dry-run count
        # would understate what a real invocation now does.
        for ticker, series in panel.series.items():
            if ticker in candidates:
                continue
            row = len(series.dates) - 1
            if row >= panel.min_history and series.dates[row] < terminal_date64:
                candidates[ticker] = row
        for ticker, row in candidates.items():
            if ticker in already_forecast:
                continue
            if np.isfinite(panel.series[ticker].features[row]).all():
                count += 1
    return count


def _dollar(log_return: float, ref_close: float) -> float:
    # A divergent model could in principle emit a non-finite or extreme
    # log-return; math.exp() raises OverflowError past roughly +/-709,
    # which would crash daily artifact generation on one bad forecast
    # (Antigravity review: "Uncaught OverflowError risk in _dollar()").
    # NaN -> neutral (ref_close itself); +/-inf and other extreme values
    # clamp to an implausibly-wide +/-50 log-return band (~5e21x) so this
    # conversion never raises -- a value this extreme is already
    # nonsensical, the clamp only prevents a crash, not a real forecast.
    if math.isnan(log_return):
        log_return = 0.0
    log_return = max(-50.0, min(50.0, log_return))
    return ref_close * math.exp(log_return)


#: The daily CSV's explicit marker for a NaN forecast component -- printed
#: instead of a fabricated dollar figure so a user reading the export can
#: tell "the model produced no usable output" from "predicted no change"
#: (Codex review: "Reject NaN forecasts before exporting prices"). The
#: scoring path's own sanitizer (metrics._bounded_log_return) already
#: penalizes a NaN as an extreme miss for accuracy purposes; this is the
#: separate, human-facing publication path _dollar()'s NaN->0.0 clamp used
#: to silently paper over.
_INVALID_FORECAST_MARKER = "N/A"


def _dollar_cell(log_return: float, ref_close: float) -> object:
    if math.isnan(log_return):
        return _INVALID_FORECAST_MARKER
    return round(_dollar(log_return, ref_close), 4)


def _current_universe_unresolved(harness: WalkForwardHarness) -> list[dict]:
    """``outstanding[UNRESOLVED_KEY]`` entries for tickers still in the
    CURRENT (rebuilt-this-invocation) panel.

    rebind_unresolved() deliberately RETAINS an unresolved entry for a
    ticker temporarily absent from the panel -- a CSV read failure or a
    capped-universe ranking swap -- since it may return later (the
    panel's own non-blocking-missing-ticker contract). But a capped
    ``--max-tickers`` universe swap is not always temporary: the ticker
    it displaced can be gone for good, and that ticker's now-permanently-
    unresolved forecast used to be exported on every later daily CSV (and
    counted in ``predicted_tickers``) forever, even though run_meta.json's
    refreshed universe snapshot no longer lists it (Codex review: "Filter
    next-day exports to the rebuilt universe"). Filtering HERE -- what
    gets reported/exported -- rather than in rebind_unresolved() itself
    keeps the entry retained internally in case the ticker does return.
    """
    return [
        entry
        for entry in harness.outstanding.get(UNRESOLVED_KEY, [])
        if entry["ticker"] in harness.panel.series
    ]


def _write_next_day_forecasts(run_dir: Path, harness: WalkForwardHarness, last_date: str) -> Path | None:
    """Tomorrow's OHLC for all tickers, in adjusted dollars, as a ready CSV.

    Sourced from ``outstanding[UNRESOLVED_KEY]`` -- by construction, exactly
    the forecasts with no known target yet, i.e. each ticker's current
    "tomorrow" prediction -- rather than filtering every bucket by
    ``origin_date == last_date``. The two differ whenever a forecast's
    origin predates today: a ticker recovered behind the cursor (or first
    crossing the warm-up threshold there) now gets a fresh forecast queued
    under an OLDER origin date (Codex review: "Predict from ticker rows
    recovered behind the cursor"), and a multi-day catch-up invocation can
    resolve an earlier pending day's target within the same run. Either way
    the forecast still belongs in this CSV; the old origin_date filter
    would have silently dropped it.
    """
    rows = []
    for entry in _current_universe_unresolved(harness):
        row: dict[str, object] = {
            "ticker": entry["ticker"],
            "model": entry["model"],
            "origin_date": entry["origin_date"],
            "ref_close": round(entry["ref_close"], 4),
        }
        for component in COMPONENTS:
            row[f"pred_{component}"] = _dollar_cell(entry["point"][component], entry["ref_close"])
            for key in QUANTILE_KEYS:
                row[f"{component}_{key}"] = _dollar_cell(entry["quantiles"][component][key], entry["ref_close"])
        rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda row: (row["ticker"], row["model"]))
    daily_dir = run_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    path = daily_dir / f"{last_date}_next_day_forecasts.csv"
    # Built in memory and published via _atomic_write_text, not a direct
    # write: a kill mid-write left a truncated CSV at this path, and the
    # NOOP-with-existing-summary path above never repairs it (only a
    # rebound or a missing SUMMARY triggers regeneration) -- the same
    # partial-artifact risk _atomic_write_text already closed for
    # <date>_summary.json (Codex review: "Replace the next-day forecast
    # CSV atomically").
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write_text(path, buffer.getvalue())
    return path


def _day_scores(
    run_dir: Path, harness: WalkForwardHarness, last_date: str, *, records: list[dict] | None = None
) -> dict:
    """Per-model summary of the scores that matured on the processed day(s).

    ``records`` -- when given, the score/forecast records the JUST-COMPLETED
    harness.run() call appended THIS invocation -- lets this filter in
    memory instead of rescanning the whole ledger from disk. On a full-
    universe replay the score ledger holds tens of millions of records, and
    without this the "one-day live cycle" decompressed and parsed every one
    of them on every single invocation just to find today's handful (Codex
    review: "Avoid rescanning the full score ledger after every daily
    run"). ``records=None`` (the artifact-recovery / rebind-only NOOP
    paths, which don't run a fresh step()) falls back to the original full
    scan -- rare enough not to matter.
    """
    summary: dict[str, dict] = {}
    if records is not None:
        by_model: dict[str, list[dict]] = {}
        for record in records:
            if record["record_type"] == "score" and record["target_date"] == last_date:
                by_model.setdefault(record["model"], []).append(record)
        model_rows = {model: by_model.get(model, []) for model in harness.config.models}
    else:
        ledger = Ledger(run_dir)
        # Shards are keyed by ORIGIN year, not target year, and a ticker can
        # resume trading after a gap of more than one year -- its forecast
        # then sits in an origin shard older than "current or prior year",
        # the only ones a New-Year-aware guess would read. Scan every
        # origin-year shard and filter by target_date instead of guessing
        # which years can hold it (Codex review: "Scan all origin-year
        # shards for daily scores").
        model_rows = {
            model: [
                record
                for record in ledger.iter_records(model=model, kind="score")
                if record["target_date"] == last_date
            ]
            for model in harness.config.models
        }
    for model in harness.config.models:
        rows = model_rows[model]
        n = len(rows)
        abs_close = sum(record["score"]["abs_err"]["close"] for record in rows)
        in_band = sum(1 for record in rows if record["score"]["in_band"]["close"])
        running = harness.running[model]
        summary[model] = {
            "scored_today": n,
            "close_mae_today": (abs_close / n) if n else None,
            "in_band_today": (in_band / n) if n else None,
            "coverage_to_date": (running.in_band_close / running.n) if running.n else None,
            "beats_persistence": None,  # filled below when persistence ran
        }
    persistence = summary.get("persistence")
    # `is not None`, not truthiness: an exact 0.0 close MAE (a flat
    # single-ticker archive) is a valid zero-error baseline, not a missing
    # one -- the old truthy check silently skipped every beats_persistence
    # comparison in that case instead of correctly marking another
    # model's positive error as not beating it (Codex review: "Compare
    # against a zero-error persistence baseline").
    if persistence and persistence["close_mae_today"] is not None:
        for model, block in summary.items():
            if model != "persistence" and block["close_mae_today"] is not None:
                block["beats_persistence"] = block["close_mae_today"] < persistence["close_mae_today"]
    return summary


def _encoder_health(run_dir: Path, harness: WalkForwardHarness) -> dict:
    ledger = Ledger(run_dir)
    for model in harness.config.models:
        retrained_after = metrics_module.latest_retrain_date(ledger.iter_records(model=model, kind="retrain"))
        records = ledger.iter_records(model=model, kind="diagnostic")
        verdict = metrics_module.diagnostic_health(records, retrained_after=retrained_after)
        if verdict["modes"]:
            return {"model": model, **verdict}
    return {"status": "N/A", "modes": {}}


def _emit_daily_artifacts(
    run_dir: Path,
    harness: WalkForwardHarness,
    config: HarnessConfig,
    last_date: str,
    rebound: int,
    days_processed: list[str],
    *,
    new_records: list[dict] | None = None,
) -> None:
    """Write daily/<date>_summary.json + the next-day forecasts CSV, and
    print the one-glance digest. Shared by the normal processed-day path and
    the NOOP recovery path below (Codex review: "Retry daily artifact
    generation after a completed run"). ``new_records`` -- see
    _day_scores() -- lets the normal path avoid a full ledger rescan."""
    scores = _day_scores(run_dir, harness, last_date, records=new_records)
    health = _encoder_health(run_dir, harness)
    forecasts_path = _write_next_day_forecasts(run_dir, harness, last_date)
    # Same selection as _write_next_day_forecasts(): a recovered or newly
    # warmed-up ticker's forecast deliberately keeps its OWN (older)
    # origin_date, not today's last_date, and the CSV already counts it as
    # a current forecast -- the old origin_date == last_date filter could
    # under-report (even zero out) predicted_tickers and the digest line
    # while the CSV it describes was non-empty (Codex review: "Count
    # recovered forecasts in the daily summary"). Also excludes tickers no
    # longer in the current panel (see _current_universe_unresolved()), so
    # this count matches what the CSV actually contains, not raw
    # outstanding[UNRESOLVED_KEY] length (Codex review: "Filter next-day
    # exports to the rebuilt universe").
    n_predicted = len(_current_universe_unresolved(harness)) // max(1, len(config.models))
    summary = {
        "date": last_date,
        "days_processed": days_processed,
        "rebound_forecasts": rebound,
        "models": scores,
        "encoder_health": health,
        "next_day_forecasts_csv": str(forecasts_path) if forecasts_path else None,
        "predicted_tickers": n_predicted,
    }
    daily_dir = run_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        daily_dir / f"{last_date}_summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )

    # The one-glance digest: a scheduled run's log should be self-explanatory.
    primary = next((name for name in config.models if name != "persistence"), config.models[0])
    block = scores.get(primary, {})
    parts = [f"[{primary}] scored {block.get('scored_today', 0)} forecasts for {last_date}"]
    if block.get("close_mae_today") is not None:
        parts.append(f"close MAE {block['close_mae_today']:.4f}")
    if block.get("in_band_today") is not None:
        parts.append(f"{block['in_band_today']:.0%} inside bands")
    if block.get("beats_persistence") is not None:
        parts.append(f"beats persistence {'yes' if block['beats_persistence'] else 'NO'}")
    parts.append(f"encoder health {health['status']}")
    parts.append(f"predicted next day for {n_predicted} tickers")
    print(": ".join([parts[0], ", ".join(parts[1:])]))
    if health["status"] == "DEGRADED":
        print(
            "encoder health DEGRADED: consider a recovery retrain "
            f"(run_walkforward.py --resume {run_dir} --retrain)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (Refused, HarnessError, LedgerError, TrainingDataChanged) as error:
        message = str(error)
        print(message if message.startswith("REFUSED") else f"REFUSED: {message}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- CLI boundary, same contract as run_walkforward
        print(f"RUNTIME FAILURE ({error.__class__.__name__}): {error}", file=sys.stderr)
        print("The last checkpoint is retained; rerun to continue.", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    import pandas as pd

    # Installed unconditionally, before the panel rebuild and
    # WalkForwardHarness.resume() -- both can run for a while (a large
    # archive, or a GBM refit-on-load) and neither was signal-protected
    # before this point; a SIGTERM arriving during either follows Python's
    # default disposition (immediate kill, no checkpoint) and a SIGINT
    # raises KeyboardInterrupt, which main()'s (Refused, HarnessError,
    # LedgerError, TrainingDataChanged) handler does not catch -- both lose
    # the cycle instead of the documented clean-checkpoint-and-exit
    # contract run_walkforward.py already has for its own equivalent setup
    # (Codex review: "Install daily signal handlers before rebuilding or
    # resuming"). Harmless on the --dry-run early-return path below: dry
    # run only reads, and an early interrupt there just exits like any
    # other cancelled read.
    interrupt_flag = _install_early_signal_handlers()

    run_dir = Path(args.run_dir)
    config = _load_stored_config(run_dir)
    meta = _load_run_meta(run_dir)
    selection = _load_selection(meta)
    # Prefer the archive the run was ORIGINALLY built from over the
    # machine's configured default: the documented daily command supplies
    # only --run-dir, and silently falling back to a different/stale
    # default archive would score and train against the wrong dataset
    # instead of refusing (Codex review: "Reuse the run's history directory
    # for daily updates"). An explicit --history-dir still wins.
    history_dir = (
        args.history_dir
        if args.history_dir is not None
        else (_load_history_dir(meta) or Path(configured_ticker_history_dir()))
    )
    if not history_dir.is_dir():
        raise Refused(f"history dir {history_dir} does not exist")
    try:
        panel = features.build_panel(
            history_dir,
            tickers=selection.get("tickers"),
            max_tickers=selection.get("max_tickers"),
            as_of=pd.Timestamp(_parse_now(args.now)) if args.now else None,
            # Guarded: pd.Timestamp(None) is NaT, not None, and would silently
            # break the normalization window (see features._fit_normalization's
            # matching guard). Stored configs from the CLI always carry a value,
            # but the guard keeps a hand-built checkpoint honest too.
            initial_train_end=(
                pd.Timestamp(config.initial_train_end) if config.initial_train_end is not None else None
            ),
            min_history=config.min_history,
        )
    except ValueError as error:
        # build_panel() raises plain ValueError for an unusable-archive
        # refusal (message already starts with "REFUSED: no usable
        # tickers"); left uncaught here, it falls through to main()'s
        # generic Exception handler and reports exit 2 "RUNTIME FAILURE"
        # instead of the documented exit 1 "REFUSED" for a data problem, the
        # same gap run_walkforward.py's _build_panel() already closed
        # (Codex review: "Classify unusable daily archives as refusals").
        raise Refused(str(error)) from error
    # A daily --now earlier than the checkpoint's last processed date (or a
    # transiently shrunk archive) would otherwise resume against a SHORTER
    # panel, re-anchoring the cursor earlier than what was already
    # processed; a later normal invocation then reprocesses those dates and
    # appends duplicate forecasts/scores -- the same failure mode
    # run_walkforward.py's --resume path already guards against (Codex
    # review: "Reject daily panels that end before the checkpoint cursor").
    # Run BEFORE both the dry-run branch below and refresh_run_meta_universe():
    # this check is read-only (a single harness_state.json read, no ledger
    # or checkpoint mutation), so running it before dry-run's own early
    # return doesn't violate dry-run's "nothing written" contract -- but
    # skipping it there used to mean a --now earlier than the checkpoint's
    # cursor (or a transiently shrunk archive) reported "would be a NOOP"
    # or "would process N days" from --dry-run while the equivalent real
    # invocation immediately refused, the opposite of what dry-run exists
    # to preview (Codex review: "Validate truncated panels during daily
    # dry runs"). Also run before refresh_run_meta_universe() overwrites
    # run_meta.json in place: if THIS invocation is about to be refused,
    # the frozen metadata must not be mutated with an invalid panel's
    # (truncated) universe on the way out (Codex review: "Refresh run
    # metadata only after resume validation").
    _verify_resume_not_before_cursor(run_dir, panel)
    if args.dry_run:
        # verify_checkpoint_integrity(mutate_ledger=False): the SAME
        # config-hash, ledger shard SHA-256, and model artifact
        # fingerprint checks the real invocation performs before it does
        # any work, routed through Ledger.verify_shards() instead of
        # Ledger.resume() so this preview cannot truncate an oversized
        # shard or delete an unrecorded one -- a command advertised as
        # "write nothing" must not perform resume()'s recovery mutations
        # (Codex review: "Keep daily dry runs non-destructive"). Reading
        # only harness_state.json unverified here (the previous version
        # of this branch) let a checkpointed ledger shard or model
        # artifact that was missing, truncated, or hash-mismatched be
        # silently approved by --dry-run even though the equivalent real
        # invocation would immediately refuse (Codex review: "Verify
        # checkpoint integrity during daily dry runs").
        state, _dry_config, _dry_ledger, _dry_checkpoint_dir = WalkForwardHarness.verify_checkpoint_integrity(
            panel, run_dir, config, strict_model_data=False, mutate_ledger=False
        )
        cursor = _cursor_from_checkpoint_state(state, panel, config)
        pending_dates = [
            str(np.datetime_as_string(panel.calendar[position], unit="D"))
            for position in range(cursor, len(panel.calendar))
        ]
        if not pending_dates:
            rebinds = _count_pending_rebinds(state, panel)
            if rebinds:
                print(
                    f"would rebind and score {rebinds} previously-unresolved forecast(s): no pending "
                    "calendar dates, but the archive now knows their target date"
                )
            else:
                print("would be a NOOP: newest archive day already processed")
        else:
            print(f"would process {len(pending_dates)} day(s): {', '.join(pending_dates)}")
        print("dry run: nothing written, nothing recovered")
        return 0
    # Confirmed-NOOP short circuit: verify the checkpoint's cheap,
    # read-only integrity checks FIRST -- everything
    # WalkForwardHarness.resume() checks before the expensive part (a
    # model's own load(), which deterministically refits gbm_quantile):
    # config-hash sanity, resume-safe-field validation, the ledger's
    # SHA-256 shard verification, and model artifact FILE fingerprints --
    # then decide from that VERIFIED state whether there is any work to
    # do. Reading harness_state.json unverified here could report NOOP
    # even when a ledger shard or model artifact was corrupted or
    # tampered with after the day's summary was written, postponing the
    # REFUSED signal a scheduled retry should have surfaced immediately
    # (Codex review: "Validate checkpoint integrity before returning
    # NOOP"). strict_model_data=False matches the real resume() call
    # below (the daily cycle's own non-blocking-missing-ticker contract)
    # -- it does not relax the ledger/model-artifact fingerprint checks
    # themselves, only the panel-history fingerprint GBM's refit-on-load
    # otherwise enforces.
    #
    # Run BEFORE refresh_run_meta_universe(), same reasoning as
    # _verify_resume_not_before_cursor() above: that call overwrites
    # run_meta.json in place, and if the checkpoint is about to be
    # REFUSED here, the frozen metadata must not be mutated with a
    # universe snapshot from a panel that was never actually applied
    # (Codex review: "Validate the checkpoint before refreshing run
    # metadata").
    # Acquired BEFORE verify_checkpoint_integrity() below: that call's own
    # Ledger.resume() truncates any ledger bytes appended past the
    # checkpoint boundary, exactly the kind of mutation a concurrent
    # replay/daily-update process must not race (Codex review: "Acquire
    # the run lock before the NOOP integrity pass"). Released here on a
    # NOOP return or on any failure before resume() is reached; handed to
    # WalkForwardHarness.resume()'s existing_lock parameter below on the
    # not-a-NOOP path instead of releasing and re-acquiring (a window
    # another process could win) -- resume() then owns it from that point
    # on and this function must not try to release it again.
    lock_handle = _acquire_run_lock(run_dir)
    try:
        noop_state, _noop_config, _noop_ledger, _noop_checkpoint_dir = (
            WalkForwardHarness.verify_checkpoint_integrity(panel, run_dir, config, strict_model_data=False)
        )
        # Keep run_meta.json's universe snapshot current as the daily cycle's
        # re-applied selection rule admits newly eligible tickers (or swaps
        # members of a capped longest-history universe) -- otherwise
        # write_report() and inspect_run.py's ribbon-ticker fallback would
        # read the ORIGINAL fit-time snapshot forever (Codex review: "Keep
        # report universe metadata current as tickers join").
        refresh_run_meta_universe(run_dir, panel)
        noop_cursor = _cursor_from_checkpoint_state(noop_state, panel, config)
        if noop_cursor >= len(panel.calendar) and not _count_pending_rebinds(noop_state, panel):
            last = (
                str(np.datetime_as_string(panel.calendar[-1], unit="D")) if len(panel.calendar) else "nothing"
            )
            summary_path = run_dir / "daily" / f"{last}_summary.json"
            if last != "nothing" and _daily_artifacts_are_complete(summary_path):
                print(f"NOOP: {last} already processed")
                _release_run_lock(lock_handle)
                return 0
    except BaseException:
        _release_run_lock(lock_handle)
        raise
    registry = _model_registry(config.models)
    # strict_model_data=False: a daily archive rebuild can transiently skip
    # a previously-trained ticker (CSV missing/unreadable that day), and
    # GbmQuantileModel.load()'s strict fingerprint check does not
    # distinguish "a ticker is temporarily absent" from "the archive was
    # retroactively corrected" -- with the default strict=True, either one
    # refuses the ENTIRE daily cycle for every model, not just this
    # ticker, directly contradicting the documented non-blocking-missing-
    # ticker contract (Codex review: "Keep missing tickers from blocking
    # GBM daily resumes"). The encoder-health diagnostic and daily digest
    # remain the detection path for genuine archive corrections in this
    # routine live cycle; --resume --retrain in run_walkforward.py is the
    # strict, operator-driven recovery path.
    harness = WalkForwardHarness.resume(
        panel, registry, run_dir, config, strict_model_data=False, existing_lock=lock_handle
    )
    if interrupt_flag.requested:
        # A signal arrived during the (now-complete) panel rebuild or
        # resume -- honor it instead of losing it, same as
        # run_walkforward.py's _run() seeds a harness from its own early
        # flag.
        harness.request_interrupt()
    _install_signal_handlers(harness)
    rebound = harness.rebind_unresolved()

    pending = len(panel.calendar) - harness.cursor
    if pending <= 0:
        if rebound:
            # With no calendar step left this invocation, rebind_unresolved()
            # scored those entries IMMEDIATELY (see harness.py's
            # end-of-calendar path) -- appending to the ledger and mutating
            # calibrator/running/outstanding state. Without a checkpoint
            # here, the next invocation's Ledger.resume() truncates that
            # just-appended score back to the OLD fingerprint and reloads
            # the OLD in-memory state, discarding the realization every
            # single time (Codex review: "Checkpoint immediate rebound
            # scores before returning NOOP").
            harness.checkpoint()
        last = (
            str(np.datetime_as_string(panel.calendar[-1], unit="D")) if len(panel.calendar) else "nothing"
        )
        # A prior invocation may have advanced the cursor (harness.run()
        # checkpointed) and then been killed before the daily artifacts were
        # written -- recover them instead of NOOPing forever (Codex review:
        # "Retry daily artifact generation after a completed run"). The full
        # multi-day list from that run is gone, so days_processed here is
        # just the one day we can still name. A rebound above also forces
        # regeneration even when the summary already exists: it just
        # appended a score and mutated calibrator/running/outstanding
        # state, and an existing summary.json/digest from before that
        # would otherwise stay silently stale forever (Codex review:
        # "Regenerate daily artifacts after immediate rebounds").
        summary_path = run_dir / "daily" / f"{last}_summary.json"
        artifacts_complete = _daily_artifacts_are_complete(summary_path)
        if last != "nothing" and (rebound or not artifacts_complete):
            reason = "a rebound updated the checkpoint" if rebound and artifacts_complete else (
                "recovering missing daily artifacts"
            )
            print(f"NOOP: {last} already processed; {reason}")
            _emit_daily_artifacts(run_dir, harness, config, last, rebound, [last])
            return 0
        print(f"NOOP: {last} already processed")
        return 0
    pending_dates = [
        str(np.datetime_as_string(panel.calendar[self_pos], unit="D"))
        for self_pos in range(harness.cursor, len(panel.calendar))
    ]

    # Capture the records THIS invocation's own harness.run() appends, so
    # _day_scores() can filter in memory afterward instead of rescanning the
    # whole ledger from disk (Codex review: "Avoid rescanning the full
    # score ledger after every daily run").
    new_records: list[dict] = []
    original_append = harness.ledger.append

    def _capturing_append(records):
        new_records.extend(records)
        return original_append(records)

    harness.ledger.append = _capturing_append
    # release_lock=False: _emit_daily_artifacts() below reads the ledger/
    # checkpoint this run just wrote (CSV + summary generation), and a
    # second process must not begin mutating this run directory while that
    # read is in progress (Codex review: "Hold the run lock through
    # artifact publication"). lock_handle is the SAME handle resume() was
    # handed via existing_lock above, so releasing it here (in the finally,
    # on every exit path) is what run()'s own release_lock() would have
    # done, just deferred past artifact publication.
    status = harness.run(release_lock=False)
    try:
        if status == "interrupted":
            print("interrupted cleanly; checkpoint saved")
            return 3
        last_date = pending_dates[-1]
        _emit_daily_artifacts(run_dir, harness, config, last_date, rebound, pending_dates, new_records=new_records)
        return 0
    finally:
        _release_run_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
