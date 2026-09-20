"""The walk-forward engine: reveal -> score -> update -> predict, forever.

One :class:`WalkForwardHarness` instance drives every model through the same
protocol per global trading-calendar date t:

1. **Reveal & score** -- forecasts whose target date is t are matched to the
   now-known actuals, scored (metrics.score_forecast), fed to the conformal
   calibrator, and ledgered as ``score`` records.
2. **Diagnostic** -- models with an encoder reconstruct the most recent
   closed day (a sampled subset of tickers, cost-capped) and the per-mode
   errors are ledgered as ``diagnostic`` records. Telemetry only.
3. **Online update** -- the revealed days accumulated since the last update
   are handed to every model's ``update()`` on the configured cadence.
4. **Predict** -- every eligible ticker (traded today, >= min_history prior
   rows, finite features) gets a next-day forecast from every model;
   calibrated + raw bands are ledgered as ``forecast`` records and the
   forecast is parked in ``outstanding`` until its target date arrives.
   A forecast made from a ticker's final row has no known target yet: it is
   ledgered with a null target date (that IS "tomorrow's forecast" -- the
   headline output) and parked under the unresolved key, re-bound to a real
   target date when a later panel reveals the next trading row (the live
   daily cycle) or left unresolved if the ticker never trades again.
5. **Checkpoint** -- every ``checkpoint_every`` steps and at every exit
   path. Two-phase (write to ``checkpoint.tmp``, swap directories) so a
   kill mid-checkpoint always leaves a valid previous checkpoint; the
   ledger's shard fingerprints ride inside, giving the truncate-or-refuse
   resume contract documented in ledger.py.

Determinism: every stochastic step draws from a seed derived as
``sha256(master_seed | iso_date | purpose)`` -- a resumed run and a
straight-through run draw identical randomness, which is what the
resume-equals-uninterrupted test asserts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

# fcntl is POSIX-only; importing it unconditionally at module level made
# harness.py -- and therefore run_walkforward.py/daily_update.py, which
# import it -- fail with ModuleNotFoundError on Windows before a single
# line of the tool could run, not merely lose locking there (Codex
# review: "Replace the POSIX-only lock dependency"). msvcrt is stdlib's
# own Windows file-locking primitive, keeping this dependency-free.
try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore[assignment]
else:
    msvcrt = None  # type: ignore[assignment]

import numpy as np
from data import COMPONENTS, TrainingPanel
from ledger import Ledger
from metrics import score_forecast
from model_interface import (
    ConformalCalibrator,
    ForecastRequest,
    make_request,
    make_revealed,
    repair_band_geometry,
)

SCHEMA_VERSION = 1
_STATE_FILENAME = "harness_state.json"
#: Config fields a resume may legitimately change: extending the horizon is
#: the point of resuming, and the circuit-breaker budgets are per-INVOCATION
#: ceilings, not properties of the run.
_RESUME_SAFE_FIELDS = frozenset({"end", "max_steps_per_run", "max_hours_per_run"})
#: Outstanding-map key for forecasts whose target date is not yet known.
UNRESOLVED_KEY = ""

#: Feature columns holding the day's own bar (the diagnostic target).
_BAR_FEATURE_SLICE = slice(0, 4)

#: Sentinel for a non-finite reconstruction-diagnostic error (a NaN/inf
#: model output, e.g. from numeric instability in the encoder). A large-
#: but-finite bound instead of the raw NaN/inf: diagnostic_health()'s
#: "current > degrade_factor * baseline" comparison always evaluates False
#: against NaN, so an unsanitized non-finite error would silently report
#: OK -- exactly when the health check is most needed (Codex review:
#: "Reject non-finite encoder diagnostics"). Clamping to a visibly-bad
#: finite value lets it correctly pull the rolling average toward DEGRADED
#: instead of vanishing.
_DIAGNOSTIC_ERROR_BOUND = 50.0


def derive_seed(master_seed: int, iso_date: str, purpose: str) -> int:
    """Deterministic per-(day, purpose) seed; identical under resume."""
    digest = hashlib.sha256(f"{master_seed}|{iso_date}|{purpose}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _setup_wallclock_tripped(config: "HarnessConfig", started_monotonic: float) -> bool:
    """Wall-clock-only circuit-breaker check, usable during fresh()'s
    initial-fit loop and resume()'s per-model load loop -- BEFORE a
    harness instance (and thus its step-ceiling-aware _breaker_tripped())
    necessarily exists yet.

    Checked before EACH model's fit()/load() call, not just once before
    the whole loop: a multi-model run (persistence + gbm_quantile +
    octopus) can otherwise blow through --max-hours entirely during setup
    with no check at all until run()'s first step, and --max-hours 0
    specifically (an accepted, documented value) should refuse before
    fitting/loading anything (Codex review: "Enforce the wall-clock
    ceiling during model setup"). This is a scoped mitigation, not a full
    fix: a SINGLE model's own fit()/load() call still cannot be
    interrupted mid-call without cooperative cancellation support in the
    OHLCForecaster protocol itself, which is out of scope here.
    """
    if config.max_hours_per_run is None:
        return False
    elapsed_hours = (time.monotonic() - started_monotonic) / 3600.0
    return elapsed_hours >= config.max_hours_per_run


def _fsync_dir(directory: Path) -> None:
    """fsync a directory's own entries (not its files' contents).

    A file's presence under a directory is metadata the OS can buffer
    separately from the file's data, so fsyncing every file inside a
    directory does not by itself guarantee the directory listing survives
    a crash too -- POSIX requires fsyncing the directory's own file
    descriptor for that (only openable read-only; there is no write mode
    for a directory).

    A no-op on Windows (fcntl is None -- the same platform check
    _acquire_run_lock()/_release_run_lock() already use): os.open() with
    O_RDONLY on a directory is a POSIX-only operation there and raises
    OSError, so every checkpoint used to fail immediately after model
    fitting on that platform (Codex review: "Make checkpoint directory
    syncing portable"). There is no equivalent reachable from the stdlib
    os module on Windows; skipping leaves this an unenforced best-effort
    durability step there, same as everywhere else this module already
    trades strict POSIX guarantees for staying dependency-free.
    """
    if fcntl is None:  # pragma: no cover - exercised on Windows only
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fingerprint_directory(directory: Path) -> dict[str, dict[str, Any]]:
    """{filename: {size, sha256}} for every file directly under ``directory``.

    Model checkpoints (metadata.json + weights.npz, where present -- GBM
    writes metadata only) determine every resumed prediction just as much
    as the ledger does, but only the ledger's shards were fingerprinted;
    a replaced or bit-flipped model file would resume silently onto
    different weights or residual buffers with no audit signal (Codex
    review: "Fingerprint checkpointed model artifacts").
    """
    fingerprints: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            data = path.read_bytes()
            fingerprints[path.name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return fingerprints


def _checkpoint_has_nan(checkpoint_dir: Path) -> bool:
    """Check if any model artifact under checkpoint_dir contains non-finite values (NaN or Inf)."""
    models_dir = checkpoint_dir / "models"
    if not models_dir.is_dir():
        return False
    for model_path in models_dir.iterdir():
        weights_path = model_path / "weights.npz"
        if weights_path.is_file():
            try:
                with np.load(weights_path) as data:
                    for key in data.files:
                        arr = data[key]
                        if np.issubdtype(arr.dtype, np.floating) and (np.isnan(arr).any() or np.isinf(arr).any()):
                            return True
            except Exception:
                return True
    return False


def _fingerprint_panel_history(panel: TrainingPanel, as_of_date: str | None) -> str:
    """Hash of every ticker's ALREADY-PROCESSED rows (dates <= ``as_of_date``),
    per ticker.

    A strict resume verifies the checkpoint's models against this, comparing
    the SAME ``as_of_date`` (the checkpoint's own recorded cursor) against
    the resumed panel: growing the archive with NEW rows past that date
    (the normal daily/replay resume case) never changes the hash, but an
    archive correction/backfill that changes a row AT OR BEFORE it does.

    Only GBM refits from the panel on load and so fingerprints its own
    training matrix; persistence and Octopus restore their exact saved
    weights/residuals directly and had no equivalent check -- a corrected
    archive (same ticker set, changed bar values) would silently resume
    them onto stale state that no longer matches the panel they are about
    to predict from (Codex review: "Verify archive contents before
    restoring model state").

    Hashes ``dates`` and ``day_of_week`` alongside ``features``: an archive
    correction that shifts historical dates or weekday assignments WITHOUT
    changing any numeric feature byte (e.g. moving a block of bars between
    two gaps that both remain clipped at the same length) left the old
    features-only hash unchanged. Octopus consumes dates/day_of_week
    directly (day-of-week embeddings, and the replay buffer resolves
    samples by date), so a mismatch there is exactly the kind of silent
    training-input drift this fingerprint exists to catch (Codex review:
    "Fingerprint all persisted model inputs").
    """
    hasher = hashlib.sha256()
    if as_of_date is None:
        return hasher.hexdigest()  # nothing processed yet -- nothing to fingerprint
    cutoff64 = np.datetime64(as_of_date)
    for ticker in panel.tickers:  # panel.tickers is already sorted -- deterministic order
        series = panel.series.get(ticker)
        if series is None:
            continue
        n_seen = int(np.searchsorted(series.dates, cutoff64, side="right"))
        if n_seen == 0:
            continue
        hasher.update(ticker.encode("utf-8"))
        hasher.update(series.features[:n_seen].tobytes())
        hasher.update(series.dates[:n_seen].tobytes())
        hasher.update(series.day_of_week[:n_seen].tobytes())
    return hasher.hexdigest()


@dataclass
class HarnessConfig:
    start: str
    initial_train_end: str | None = None
    end: str | None = None
    models: tuple[str, ...] = ("persistence", "gbm_quantile")
    window_len: int = 128
    min_history: int = 60
    update_every: int = 1
    checkpoint_every: int = 20
    diagnostic_sample: int = 500
    seed: int = 0
    calibration: bool = True
    #: Circuit-breaker ceilings for ONE invocation (production-safe-loop
    #: pattern: hard limits, checked pre-flight, tripping loudly). None =
    #: unbounded; a trip checkpoints cleanly and the run resumes later.
    max_steps_per_run: int | None = None
    max_hours_per_run: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["models"] = list(self.models)
        return payload

    def config_hash(self) -> str:
        payload = {key: value for key, value in self.as_dict().items() if key not in _RESUME_SAFE_FIELDS}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class HarnessError(RuntimeError):
    """Raised on refusal paths (config mismatch, missing checkpoint, ...)."""


#: Per-run-directory advisory lock filename (fcntl.flock, POSIX).
_LOCK_FILENAME = "harness.lock"


def _acquire_run_lock(run_dir: Path):
    """Exclusive, non-blocking lock on ``run_dir``, held for the lifetime
    of a WalkForwardHarness constructed via fresh() or resume().

    Without this, two replay/daily-update processes both targeting the
    same --run-dir can open the SAME checkpoint boundary concurrently:
    one process's Ledger.resume() can truncate or delete shards the
    other just appended, both can rename away each other's
    checkpoint.tmp mid-swap, or records can be duplicated -- silently
    corrupting the run despite the daily command's documented idempotence
    (Codex review: "Serialize mutations of each run directory"). Fail
    fast instead: a second process REFUSES immediately rather than
    racing the first.

    Released in run()'s finally block (covering every return path:
    completed, interrupted, budget-tripped) or, if run() is never called,
    when the harness object is garbage collected and its file handle
    closes. daily_update.py's standalone verify_checkpoint_integrity()
    NOOP pre-check also acquires this same lock before that call (Codex
    review: "Acquire the run lock before the NOOP integrity pass") --
    that call's own Ledger.resume() truncates any ledger bytes appended
    past the checkpoint boundary, exactly the kind of mutation this lock
    exists to serialize -- and hands it to WalkForwardHarness.resume()'s
    existing_lock parameter on the not-a-NOOP path rather than releasing
    and re-acquiring (a gap another process could win).
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    handle = (run_dir / _LOCK_FILENAME).open("a+")
    try:
        # Explicit seek(0): msvcrt.locking() locks a byte range starting
        # at the CURRENT file position, so lock and unlock must agree on
        # that position -- "a+" mode's initial position is not guaranteed
        # to be 0 on every platform. A no-op for fcntl.flock, which locks
        # the whole file regardless of position.
        handle.seek(0)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:  # pragma: no cover - exercised on Windows only
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        raise HarnessError(
            f"REFUSED: another process already holds the lock on {run_dir}; only one "
            "replay/daily-update process may operate on a run directory at a time"
        ) from exc
    return handle


def _release_run_lock(handle) -> None:
    """Counterpart to _acquire_run_lock(): unlocks (Windows requires an
    explicit unlock before close; POSIX releases on close regardless,
    but this keeps both platforms' semantics visible and symmetric) and
    closes the handle."""
    if fcntl is None and msvcrt is not None:  # pragma: no cover - exercised on Windows only
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    handle.close()


@dataclass
class _RunningStats:
    """Cheap per-model progress counters for the one-line-per-step log."""

    n: int = 0
    in_band_close: int = 0
    abs_close: float = 0.0

    def add(self, score: dict[str, Any]) -> None:
        self.n += 1
        self.in_band_close += 1 if score["in_band"]["close"] else 0
        self.abs_close += score["abs_err"]["close"]

    def as_dict(self) -> dict[str, Any]:
        return {"n": self.n, "in_band_close": self.in_band_close, "abs_close": self.abs_close}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "_RunningStats":
        return cls(n=payload["n"], in_band_close=payload["in_band_close"], abs_close=payload["abs_close"])


class WalkForwardHarness:
    def __init__(
        self,
        panel: TrainingPanel,
        models: dict[str, Any],
        run_dir: Path,
        config: HarnessConfig,
        ledger: Ledger,
        *,
        log: Callable[[str], None] = print,
        invocation_started_monotonic: float | None = None,
    ) -> None:
        self.panel = panel
        self.models = models
        self.run_dir = Path(run_dir)
        self.config = config
        self.ledger = ledger
        self.log = log
        self.cursor = 0
        self.step_index = 0
        self.outstanding: dict[str, list[dict[str, Any]]] = {}
        # [ticker, origin_row, origin_date_iso] triples awaiting an update
        # call; origin_date_iso lets a later step re-anchor origin_row by
        # date instead of trusting a checkpointed integer that a rebuilt
        # panel can invalidate (Codex review: "Re-anchor queued updates
        # after rebuilding the panel").
        self.pending_revealed: list[list] = []
        # fresh()'s ragged-ticker boundary-row seed, keyed by the row's own
        # TARGET date (label_end_date), held here -- NOT in pending_revealed
        # directly -- until step()'s cursor actually reaches that date. A
        # ragged ticker's boundary row can target a date LATER than the
        # first replay date; queuing it straight into pending_revealed used
        # to let the very first step() flush it unconditionally (step_index
        # 0 always satisfies the update_every cadence check), training the
        # model on a label from a date the walk-forward cursor had not
        # reached yet -- lookahead contaminating every forecast made in
        # between (Codex review: "Delay ragged boundary reveals until their
        # label date"). Same shape as pending_revealed's own triples.
        self._pending_seed_reveals: dict[str, list[list]] = {}
        self.calibrators: dict[str, ConformalCalibrator] = {
            name: ConformalCalibrator() for name in config.models
        }
        self.running: dict[str, _RunningStats] = {name: _RunningStats() for name in config.models}
        self._interrupted = False
        #: Set by fresh()/resume() to the open lock file handle; released
        #: by _release_lock() (called from run()'s finally, or implicitly
        #: on GC if run() is never invoked).
        self._lock_handle = None
        self._total_steps_in_run: int | None = None
        self._steps_taken_in_run: int = 0
        # This is deliberately supplied by the CLI before archive/model setup:
        # model fitting and checkpoint loading consume the same hard budget as
        # replay steps.  Direct library callers start their invocation here.
        self.invocation_started_monotonic = (
            time.monotonic() if invocation_started_monotonic is None else invocation_started_monotonic
        )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def fresh(
        cls,
        panel: TrainingPanel,
        models: dict[str, Any],
        run_dir: Path,
        config: HarnessConfig,
        *,
        log: Callable[[str], None] = print,
        invocation_started_monotonic: float | None = None,
        existing_lock: Any = None,
    ) -> "WalkForwardHarness":
        run_dir = Path(run_dir)
        # Captured BEFORE acquiring the lock below: _acquire_run_lock()
        # itself creates run_dir as a side effect (it must exist to hold
        # the lock file), which would otherwise make this always True by
        # the time it's read. With existing_lock supplied, the caller
        # already holds (and thus already created) run_dir, so this is
        # always True in that case -- the same already-established
        # tradeoff run_walkforward.py's own pre-fresh() mkdir already made
        # for the non-locked path (see its call site's comment).
        run_dir_preexisted = run_dir.exists()
        # Held for this harness's whole lifetime (transferred onto the
        # returned harness just below run()'s eventual owner releases it;
        # see _acquire_run_lock()'s docstring) so a second concurrent
        # fresh()/resume() on the same run_dir REFUSES immediately instead
        # of racing this one (Codex review: "Serialize mutations of each
        # run directory"). Acquired before the checkpoint/checkpoint.prev
        # checks below so two racing fresh() calls can't both observe
        # "doesn't exist yet" and both proceed.
        #
        # existing_lock: a caller that ALREADY holds this run_dir's lock
        # (run_walkforward.py's fresh-run dispatch acquires it before
        # deciding whether run_meta.json is safe to (re)write, then passes
        # it through here on the not-a-refusal path) hands it over instead
        # of this classmethod acquiring a SECOND, conflicting lock on the
        # same file -- flock/msvcrt locks are per OPEN FILE DESCRIPTION, so
        # a second open()+lock() from the very same process would otherwise
        # refuse against its own already-held lock (Codex review: "Acquire
        # the fresh-run lock before replacing metadata"). Mirrors resume()'s
        # existing_lock parameter exactly.
        lock_handle = existing_lock if existing_lock is not None else _acquire_run_lock(run_dir)
        try:
            # checkpoint.prev/ is the mid-swap recovery copy checkpoint()
            # leaves behind if a process dies between renaming
            # checkpoint/ -> checkpoint.prev/ and installing
            # checkpoint.tmp/ (see checkpoint()'s two-rename swap below).
            # In that exact window checkpoint/ does not exist, so checking
            # only for it would let a zero-shard-ledger fresh() proceed --
            # and its own first checkpoint() call would then rmtree the
            # still-recoverable checkpoint.prev/ before writing the
            # refit-from-scratch state, silently destroying data resume()
            # already knows how to recover (Codex review: "Refuse fresh
            # runs when checkpoint.prev is recoverable"). Either directory
            # existing means this run_dir needs resume, not fresh.
            if (
                (run_dir / "checkpoint").exists()
                or (run_dir / "checkpoint.prev").exists()
                or (run_dir / "checkpoint.good").exists()
            ):
                raise HarnessError(f"REFUSED: {run_dir} already holds a checkpoint; use resume")
            # Tracked so the cleanup below never touches a directory the
            # operator already owned -- only what THIS call itself created.
            # Checked separately for run_dir and ledger/: Ledger.create()
            # only refuses on an existing *.jsonl.gz shard, not on an
            # existing ledger/ directory holding OTHER operator files, so a
            # pre-existing (non-shard) ledger/ must survive a failed fit
            # exactly like a pre-existing run_dir does (Codex review:
            # "Preserve pre-existing files when fresh fitting fails").
            ledger_dir_preexisted = (run_dir / "ledger").exists()
            run_dir.mkdir(parents=True, exist_ok=True)  # no-op: the lock above already created it
            try:
                ledger = Ledger.create(run_dir)
                harness = cls(
                    panel, models, run_dir, config, ledger, log=log,
                    invocation_started_monotonic=invocation_started_monotonic,
                )
                harness.cursor = int(np.searchsorted(panel.calendar, np.datetime64(config.start)))
                for name, model in models.items():
                    # Checked before EVERY model, including the first: a
                    # single-model run (especially --model octopus, whose
                    # initial fit can run for hours at full-universe scale)
                    # used to exempt index 0 so fresh() could still build a
                    # checkpoint and let run()'s own pre-flight check report
                    # the trip -- but that let --max-hours 0 perform the
                    # ENTIRE initial fit anyway before ever reporting
                    # anything, directly contradicting
                    # _setup_wallclock_tripped()'s own documented contract
                    # ("--max-hours 0 ... should refuse before fitting/
                    # loading anything") and the hard-pre-flight-ceiling
                    # promise the whole circuit-breaker design makes (Codex
                    # review: "Check the wall-clock limit before the first
                    # model"). A multi-model run (persistence + gbm_quantile
                    # + octopus) has the same exposure at any index, which
                    # the original fix already covered for index > 0 (Codex
                    # review: "Enforce the wall-clock ceiling during model
                    # setup").
                    if _setup_wallclock_tripped(config, harness.invocation_started_monotonic):
                        raise HarnessError(
                            f"REFUSED: --max-hours budget exhausted during initial fit setup "
                            f"(before fitting {name!r}); increase --max-hours or use a smaller "
                            "universe/preset"
                        )
                    stats = model.fit(panel, seed=derive_seed(config.seed, config.start, f"fit:{name}"))
                    log(f"[fit] {name}: {stats}")
                # The initial fit's trainability cutoff (config.initial_train_end,
                # default the day before config.start) excludes any row whose
                # target (label_end_date) falls ON OR AFTER config.start -- but the
                # walk-forward loop's own first step() never predicts FOR that row
                # either (the loop starts predicting FROM config.start onward,
                # never backward). Without this, every ticker's newest causally-
                # available training example -- the row whose target date is
                # exactly the first REPLAY date -- would be permanently skipped by
                # both paths (Codex review: "Ingest labels revealed on the first
                # simulation date"). Seed it into pending_revealed so the first
                # update() flush absorbs it, following the documented reveal ->
                # update -> predict order; no lookahead, since this only flushes
                # once step() is actually processing that date or later.
                #
                # Anchored to the CURSOR's date, not config.start itself: config.start
                # need not be a trading date (the documented default, 2000-01-01, is
                # a Saturday), and harness.cursor -- set just above via
                # searchsorted(calendar, config.start) -- already resolves to the
                # actual first calendar date step() will process (Codex review:
                # "Seed reveals from the actual first replay date").
                if harness.cursor < len(panel.calendar):
                    start64 = panel.calendar[harness.cursor].astype("datetime64[D]")
                    for ticker, series in panel.series.items():
                        # Calendar-day comparison: load_history_csv() now normalizes
                        # every archive Date at ingestion, but a hand-built panel
                        # (tests, or any future caller that constructs a
                        # TickerSeries directly) could still carry an intraday
                        # timestamp -- defense in depth alongside the source-level
                        # fix (Codex review: "Match forecast origins/queued
                        # reveals/training references by calendar day").
                        #
                        # A RAGGED ticker (a gap, a later join, or simply not
                        # trading on the exact global first-replay date) can have
                        # its own boundary row's target land on a LATER date than
                        # start64 -- matching label_end_dates == start64 exactly
                        # missed it entirely, permanently omitting that ticker's
                        # newest pre-replay training example from every model's
                        # online training (Codex review: "Reveal pre-start labels
                        # on each ticker's first replay day"). own_date < start64
                        # <= label_end_date identifies the SAME row generally: the
                        # one row still "pre-replay" for THIS ticker whose target
                        # already reaches (or passes) the global first replay
                        # date -- consecutive rows' labels strictly increase
                        # (label[i] = dates[i+1]), so at most one row per ticker
                        # can ever satisfy both halves at once.
                        own_dates64 = series.dates.astype("datetime64[D]")
                        label_dates64 = series.label_end_dates.astype("datetime64[D]")
                        positions = np.flatnonzero((own_dates64 < start64) & (label_dates64 >= start64))
                        if not len(positions):
                            continue
                        origin_row = int(positions[-1])
                        if not np.isfinite(series.targets[origin_row]).all():
                            continue
                        # A ticker whose first archive row sits immediately before
                        # start has enough LATER rows for panel admission but its
                        # own row 0 has a finite target and all-NaN bar-return
                        # features (no prior close to compute a return from) -- the
                        # same two-part eligibility normal reveals get via
                        # eligible_on()'s warm-up rule (idx >= min_history) plus the
                        # features.isfinite() filter (see step()'s `eligible` list)
                        # must apply here too, or a NaN residual/training row
                        # reaches the model's first update() (Codex review: "Exclude
                        # untrainable rows from the seeded reveal").
                        if origin_row < panel.min_history:
                            continue
                        if not np.isfinite(series.features[origin_row]).all():
                            continue
                        origin_date_iso = str(np.datetime_as_string(series.dates[origin_row], unit="D"))
                        # Held by TARGET date, not queued into
                        # pending_revealed directly: a ragged ticker's
                        # target can be LATER than start64, and step()
                        # only releases it once the cursor actually
                        # reaches that date (Codex review: "Delay ragged
                        # boundary reveals until their label date").
                        label_date_iso = str(
                            np.datetime_as_string(series.label_end_dates[origin_row], unit="D")
                        )
                        harness._pending_seed_reveals.setdefault(label_date_iso, []).append(
                            [ticker, origin_row, origin_date_iso]
                        )
                harness.checkpoint()
            except BaseException:
                # A model's fit() (gbm_quantile's documented refusal on an
                # untrainable cutoff, or any other failure before the first
                # checkpoint()) must not leave a run-shaped directory behind:
                # Ledger.create() above already wrote <run_dir>/ledger to disk,
                # contradicting the "REFUSED -- nothing was written" contract
                # (Codex review: "Create run artifacts only after initial fits
                # succeed"). Nothing has been appended to the ledger yet (no
                # step() or checkpoint() has run), so it is always safe to
                # discard -- but ONLY if THIS call created it: removed only
                # when it didn't already exist, same as run_dir itself.
                if not ledger_dir_preexisted:
                    shutil.rmtree(run_dir / "ledger", ignore_errors=True)
                if not run_dir_preexisted:
                    shutil.rmtree(run_dir, ignore_errors=True)
                raise
            # Ownership of the lock transfers to the harness: run()
            # releases it later (see run()'s docstring comment), covering
            # every exit path of this harness's eventual replay/daily
            # invocation.
            harness._lock_handle = lock_handle
            return harness
        except BaseException:
            _release_run_lock(lock_handle)
            raise

    @classmethod
    def verify_checkpoint_integrity(
        cls,
        panel: TrainingPanel | None,
        run_dir: Path,
        requested_config: HarnessConfig,
        *,
        strict_model_data: bool = True,
        mutate_ledger: bool = True,
    ) -> tuple[dict[str, Any], HarnessConfig, Ledger | None, Path]:
        """Every read-only integrity check resume() performs BEFORE it pays
        for the expensive part (a model's own load(), which deterministically
        REFITS for gbm_quantile) -- checkpoint schema/config-hash sanity,
        resume-safe-field validation, the optional panel history fingerprint,
        ledger shard SHA-256 verification, and model ARTIFACT FILE
        fingerprints (not the models themselves).

        Callers get back everything needed to continue into the (expensive)
        per-model load loop: ``(state, config, ledger, checkpoint_dir)``.
        Split out of resume() so a caller that already knows there is no
        work to do this invocation (daily_update.py's same-day-NOOP short
        circuit) can still prove the checkpoint is intact -- and REFUSE
        instead of silently reporting NOOP -- without loading a single
        model (Codex review: "Validate checkpoint integrity before
        returning NOOP").

        mutate_ledger=False routes the ledger check through
        Ledger.verify_shards() instead of Ledger.resume(): the SAME
        missing/short/hash-mismatch verification, but without truncating
        an oversized shard or deleting an unrecorded one -- for a caller
        (daily_update.py's --dry-run preview) whose own contract forbids
        any mutation, which resume()'s recovery side effects would
        otherwise violate (Codex review: "Verify checkpoint integrity
        during daily dry runs"). The returned ledger is None in that case
        -- callers that need a usable Ledger must pass mutate_ledger=True
        (the default).
        """
        run_dir = Path(run_dir)
        checkpoint_dir = run_dir / "checkpoint"
        previous_dir = run_dir / "checkpoint.prev"
        good_dir = run_dir / "checkpoint.good"
        if not checkpoint_dir.exists():
            if previous_dir.exists():
                # A crash between the two renames of the checkpoint swap: the
                # previous checkpoint is complete and authoritative.
                if mutate_ledger:
                    previous_dir.rename(checkpoint_dir)
                else:
                    # mutate_ledger=False's caller (daily_update.py's --dry-run
                    # preview) must perform NO mutation, including this
                    # crash-recovery rename -- read straight from
                    # checkpoint.prev/ in place instead (Codex review: "Keep
                    # checkpoint fallback recovery read-only in dry runs").
                    checkpoint_dir = previous_dir
            elif good_dir.exists():
                if mutate_ledger:
                    shutil.copytree(good_dir, checkpoint_dir)
                else:
                    checkpoint_dir = good_dir

        if checkpoint_dir.exists() and _checkpoint_has_nan(checkpoint_dir):
            # Autonomous self-healing: current checkpoint contains non-finite weights (NaN/Inf).
            # Look for healthy fallback in checkpoint.good or checkpoint.prev.
            fallback_dir = None
            if good_dir.is_dir() and not _checkpoint_has_nan(good_dir):
                fallback_dir = good_dir
            elif previous_dir.is_dir() and not _checkpoint_has_nan(previous_dir):
                fallback_dir = previous_dir

            if fallback_dir is not None:
                if mutate_ledger:
                    shutil.rmtree(checkpoint_dir)
                    shutil.copytree(fallback_dir, checkpoint_dir)
                else:
                    checkpoint_dir = fallback_dir
            else:
                raise HarnessError(
                    f"REFUSED: checkpoint under {run_dir} contains non-finite weights and no healthy fallback exists"
                )

        if not checkpoint_dir.exists():
            raise HarnessError(f"REFUSED: no checkpoint under {run_dir}")
        state = json.loads((checkpoint_dir / _STATE_FILENAME).read_bytes())
        if state["schema_version"] != SCHEMA_VERSION:
            raise HarnessError(
                f"REFUSED: checkpoint schema {state['schema_version']} != supported {SCHEMA_VERSION}"
            )
        config_kwargs: dict[str, Any] = dict(state["config"])
        config_kwargs["models"] = tuple(config_kwargs["models"])
        stored_config = HarnessConfig(**config_kwargs)
        # Integrity check on the checkpoint FILE itself, independent of
        # whatever this invocation happens to request: state["config_hash"]
        # was sealed in at the write that produced this exact state["config"]
        # (checkpoint()'s two-phase write makes them a matched pair). If disk
        # corruption or a hand-edit changed state["config"] without updating
        # state["config_hash"] to match, re-deriving stored_config and only
        # comparing IT against requested_config could sail straight through
        # -- the sealed-spec contract as good as unenforced (Codex review:
        # "Verify the checkpoint's recorded config hash").
        if stored_config.config_hash() != state["config_hash"]:
            raise HarnessError(
                f"REFUSED: checkpoint under {run_dir} is corrupted -- its recorded config_hash does not "
                "match its own config; do not hand-edit harness_state.json"
            )
        if stored_config.config_hash() != requested_config.config_hash():
            stored = stored_config.as_dict()
            requested = requested_config.as_dict()
            differing = sorted(
                key
                for key in set(stored) | set(requested)
                if key not in _RESUME_SAFE_FIELDS and stored.get(key) != requested.get(key)
            )
            raise HarnessError(
                f"REFUSED: resumed flags differ from the recorded run config on {differing}; "
                "only resume-safe flags (--end/--now) may change across a resume"
            )
        # Resume-safe fields come from the REQUEST (extending --end and
        # setting this invocation's circuit-breaker budgets is the point);
        # everything else from the recorded config.
        requested = requested_config.as_dict()
        overrides = {name: requested[name] for name in _RESUME_SAFE_FIELDS}
        config = HarnessConfig(**{**stored_config.as_dict(), **overrides})
        config.models = tuple(config.models)
        # Persistence and Octopus restore their EXACT saved weights/residuals
        # on load rather than refitting from the panel (only GBM does that,
        # and fingerprints its own training matrix to catch this); an
        # archive correction/backfill to already-processed history -- same
        # ticker set, so _verify_resume_universe()'s hash misses it -- would
        # otherwise silently resume them onto state that no longer matches
        # what the panel now says those rows are. strict_model_data=False is
        # an explicit --resume --retrain: retrain() immediately refits every
        # model from the current panel anyway, so this check is skipped the
        # same way GBM's training-fingerprint check is (Codex review:
        # "Verify archive contents before restoring model state").
        recorded_panel_fingerprint = state.get("panel_fingerprint")
        if strict_model_data and recorded_panel_fingerprint is not None:
            if panel is None:
                raise HarnessError(f"REFUSED: panel is required to verify panel fingerprint under {run_dir}")
            actual_panel_fingerprint = _fingerprint_panel_history(panel, state.get("last_processed_date"))
            if actual_panel_fingerprint != recorded_panel_fingerprint:
                raise HarnessError(
                    f"REFUSED: panel history under {run_dir} reproduced DIFFERENT already-processed feature "
                    "rows than the ones the checkpoint recorded (the archive changed between checkpoint and "
                    "resume); pass --retrain for a sanctioned recovery refit, or investigate the archive change"
                )
        if mutate_ledger:
            ledger = Ledger.resume(run_dir, state["ledger"])
        else:
            Ledger.verify_shards(run_dir, state["ledger"])
            ledger = None
        # Model artifacts (metadata.json + weights.npz, where present)
        # determine every resumed prediction just as much as the ledger
        # does, but only ledger shards were fingerprinted; a replaced or
        # bit-flipped model file would otherwise resume silently onto
        # different weights or residual buffers with no audit signal
        # (Codex review: "Fingerprint checkpointed model artifacts").
        recorded_model_fingerprints = state.get("model_fingerprints")
        if recorded_model_fingerprints is None:
            raise HarnessError(f"REFUSED: checkpoint under {run_dir} has no recorded model artifact fingerprints")
        for name in config.models:
            expected = recorded_model_fingerprints.get(name)
            if expected is None:
                raise HarnessError(
                    f"REFUSED: checkpoint under {run_dir} has no recorded fingerprint for model {name!r}"
                )
            actual = _fingerprint_directory(checkpoint_dir / "models" / name)
            if actual != expected:
                raise HarnessError(
                    f"REFUSED: model artifact files for {name!r} under {run_dir} do not match the "
                    "checkpoint; do not hand-edit or replace checkpointed model files"
                )
        return state, config, ledger, checkpoint_dir

    @classmethod
    def resume(
        cls,
        panel: TrainingPanel,
        model_registry: dict[str, Any],
        run_dir: Path,
        requested_config: HarnessConfig,
        *,
        log: Callable[[str], None] = print,
        invocation_started_monotonic: float | None = None,
        strict_model_data: bool = True,
        device: str = "auto",
        existing_lock: Any = None,
    ) -> "WalkForwardHarness":
        run_dir = Path(run_dir)
        # Held for this harness's whole lifetime once resume() succeeds
        # (see _acquire_run_lock()'s docstring). Acquired BEFORE
        # verify_checkpoint_integrity() below: Ledger.resume() (called
        # inside it) truncates any ledger bytes appended past the
        # checkpoint boundary -- exactly the kind of mutation a
        # concurrent process must not race (Codex review: "Serialize
        # mutations of each run directory").
        #
        # existing_lock: a caller that ALREADY holds this run_dir's lock
        # (daily_update.py's NOOP short circuit acquires it before its own
        # standalone verify_checkpoint_integrity() call, then passes it
        # through here on the not-a-NOOP path) hands it over instead of
        # this classmethod acquiring a SECOND, conflicting lock on the same
        # file -- flock/msvcrt locks are per OPEN FILE DESCRIPTION, so a
        # second open()+lock() from the very same process would otherwise
        # refuse against its own already-held lock (Codex review: "Acquire
        # the run lock before the NOOP integrity pass").
        lock_handle = existing_lock if existing_lock is not None else _acquire_run_lock(run_dir)
        try:
            state, config, ledger, checkpoint_dir = cls.verify_checkpoint_integrity(
                panel, run_dir, requested_config, strict_model_data=strict_model_data
            )
            if ledger is None:
                raise HarnessError(f"REFUSED: could not initialize ledger for {run_dir}")
            # Same fallback __init__ applies once the harness exists: no
            # earlier anchor supplied means "start counting from now."
            # Computed here, before the harness exists, so the per-model
            # load-loop check just below has a reference point to measure
            # against (Codex review: "Enforce the wall-clock ceiling
            # during model setup").
            setup_started_monotonic = (
                time.monotonic() if invocation_started_monotonic is None else invocation_started_monotonic
            )
            models: dict[str, Any] = {}
            for name in config.models:
                # Checked before EVERY model, including the first -- same
                # reasoning as fresh()'s fit loop just above: a single-model
                # resume (e.g. a lone GBM refit-on-load) used to exempt
                # index 0, letting an already-exhausted budget perform the
                # ENTIRE load/refit anyway before ever refusing, the same
                # contradiction of _setup_wallclock_tripped()'s own
                # documented contract fresh()'s equivalent carve-out had
                # (Codex review: "Check the wall-clock limit before the
                # first model"). A MULTI-model resume (e.g. GBM's
                # refit-on-load alongside persistence/octopus) has the same
                # exposure at any index, which the original fix already
                # covered for index > 0 (Codex review: "Enforce the
                # wall-clock ceiling during model setup").
                if _setup_wallclock_tripped(config, setup_started_monotonic):
                    raise HarnessError(
                        f"REFUSED: --max-hours budget exhausted while loading checkpointed models "
                        f"(before loading {name!r}); increase --max-hours or resume with fewer models"
                    )
                model_cls = model_registry[name]
                # strict_model_data=False is an explicit --resume --retrain: a
                # model whose refit-on-load step (gbm_quantile) hits a changed
                # panel would otherwise refuse here, before retrain() ever gets
                # a chance to run -- the operator's own recovery path would then
                # be unusable (Codex review: "Detect training-data changes
                # before refitting GBM checkpoints").
                models[name] = model_cls.load(
                    checkpoint_dir / "models" / name,
                    panel_provider=lambda: panel,
                    strict=strict_model_data,
                    device=device,
                )
            assert ledger is not None
            harness = cls(
                panel, models, run_dir, config, ledger, log=log,
                invocation_started_monotonic=invocation_started_monotonic,
            )
            last_processed = state.get("last_processed_date")
            if last_processed is not None:
                # Re-anchor by date (see checkpoint()): identical to the stored
                # integer on an unchanged calendar, correct on a grown one.
                harness.cursor = int(
                    np.searchsorted(panel.calendar, np.datetime64(last_processed), side="right")
                )
            else:
                harness.cursor = int(np.searchsorted(panel.calendar, np.datetime64(config.start)))
            # Clamp to never go BEFORE the frozen prediction horizon: if the
            # rebuilt archive backfills a new calendar date between the stored
            # pre-start anchor and config.start (e.g. an already-selected
            # ticker gains its first row just before start), the date-based
            # reanchor above can land on that newly-inserted pre-start date,
            # resuming the run outside its own configured horizon (Codex
            # review: "Clamp the resumed cursor to the configured start").
            harness.cursor = max(
                harness.cursor, int(np.searchsorted(panel.calendar, np.datetime64(config.start)))
            )
            harness.step_index = state["step_index"]
            harness.outstanding = state["outstanding"]
            harness.pending_revealed = state["pending_revealed"]
            # .get(): additive field, same SCHEMA_VERSION -- absent on a
            # checkpoint written before this fix existed.
            harness._pending_seed_reveals = state.get("pending_seed_reveals", {})
            harness.calibrators = {
                name: ConformalCalibrator.from_state(payload) for name, payload in state["calibrators"].items()
            }
            harness.running = {
                name: _RunningStats.from_dict(payload) for name, payload in state["running"].items()
            }
            harness._lock_handle = lock_handle
            return harness
        except BaseException:
            _release_run_lock(lock_handle)
            raise

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------
    def request_interrupt(self) -> None:
        self._interrupted = True

    def _release_lock(self) -> None:
        """Idempotent: safe to call even if no lock was ever acquired
        (e.g. a harness constructed directly by a test, bypassing
        fresh()/resume()) or if run() releases it a second time."""
        if self._lock_handle is not None:
            _release_run_lock(self._lock_handle)
            self._lock_handle = None

    def _breaker_tripped(self, steps_taken: int, started_monotonic: float) -> str | None:
        """Pre-flight circuit breaker: checked BEFORE each step's work.

        The production-safe-loop pattern: hard per-invocation ceilings on
        step count and wall-clock, no grace period -- `steps == limit` is
        allowed, the step after trips. Post-flight checking would mean the
        budgeted work was already spent before anyone knew.
        """
        import time

        if getattr(self, "_model_failure", False):
            return "systemic_model_failure"
        if self.config.max_steps_per_run is not None and steps_taken >= self.config.max_steps_per_run:
            return "step_ceiling"
        if self.config.max_hours_per_run is not None:
            elapsed_hours = (time.monotonic() - started_monotonic) / 3600.0
            if elapsed_hours >= self.config.max_hours_per_run:
                return "wallclock_ceiling"
        return None

    def _trip_banner(self, reason: str, steps_taken: int, started_monotonic: float) -> None:
        """Loud, unmissable state dump BEFORE the clean stop -- even a caller
        that ignores the return status leaves this in the operator's log."""
        import time

        elapsed_hours = (time.monotonic() - started_monotonic) / 3600.0
        self.log("=== CIRCUIT BREAKER CHECKPOINT ===")
        self.log(f"reason        : {reason}")
        self.log(f"steps this run: {steps_taken} / limit {self.config.max_steps_per_run}")
        self.log(f"hours this run: {elapsed_hours:.2f} / limit {self.config.max_hours_per_run}")
        if reason == "systemic_model_failure":
            self.log("action        : execution HALTED; last healthy checkpoint preserved; corrupt state NOT written")
        else:
            self.log("action        : checkpoint saved; resume with --resume when ready")
        self.log("==================================")

    def run(self, *, release_lock: bool = True) -> str:
        # The per-run-directory lock (fresh()/resume()) is held for the
        # harness's whole lifetime up to this point; release it here, on
        # every return path (completed, interrupted, budget-tripped) or an
        # unexpected exception, so a NEXT invocation on this run_dir is
        # never blocked by a lock this process no longer needs (Codex
        # review: "Serialize mutations of each run directory" -- hold
        # through the final checkpoint, not beyond it).
        #
        # release_lock=False lets a caller that still has to PUBLISH
        # artifacts from this run's state -- run_walkforward.py's
        # write_report(), daily_update.py's CSV/summary writing -- keep
        # holding the lock through that work instead of a second process
        # racing in the gap between run() returning and the caller finishing
        # its reads of the ledger/checkpoint this run just wrote (Codex
        # review: "Hold the run lock through artifact publication"). Such a
        # caller must call release_lock() itself once publication is done
        # (or has failed), in its own finally.
        try:
            return self._run_locked()
        finally:
            if release_lock:
                self._release_lock()

    def release_lock(self) -> None:
        """Public counterpart to run(release_lock=False): releases the
        run-directory lock once a caller that deferred release has finished
        publishing this run's artifacts. Idempotent, like _release_lock()."""
        self._release_lock()

    def _run_locked(self) -> str:
        end64 = np.datetime64(self.config.end) if self.config.end else None
        steps_taken = 0
        started_monotonic = self.invocation_started_monotonic
        total_remaining = len(self.panel.calendar) - self.cursor
        if end64 is not None:
            cal_dates = self.panel.calendar.astype("datetime64[D]")
            end_idx = int(np.searchsorted(cal_dates, end64, side="right"))
            total_remaining = max(0, min(total_remaining, end_idx - self.cursor))
        if self.config.max_steps_per_run is not None:
            total_remaining = min(total_remaining, self.config.max_steps_per_run)
        self._total_steps_in_run = total_remaining
        self._steps_taken_in_run = 0
        while self.cursor < len(self.panel.calendar):
            date64 = self.panel.calendar[self.cursor]
            # Calendar-day comparison, not a raw timestamp one: an archive
            # can carry a non-midnight Date (load_history_csv() preserves
            # whatever time-of-day component it has), so panel.calendar can
            # too. config.end is always a plain date string (day-precision
            # once parsed), and build_panel() already normalizes ITS OWN
            # --end truncation to calendar-day granularity -- this second,
            # separate comparison did not, so a non-midnight final bar
            # (2020-12-31T16:00, say) compared > "2020-12-31" (midnight)
            # and stopped the replay before processing its own inclusive
            # end day (Codex review: "Compare the replay end by calendar
            # day").
            if end64 is not None and date64.astype("datetime64[D]") > end64:
                break
            tripped = self._breaker_tripped(steps_taken, started_monotonic)
            if tripped is not None:
                self._trip_banner(tripped, steps_taken, started_monotonic)
                if tripped == "systemic_model_failure":
                    return "systemic_model_failure"
                self.checkpoint()
                return "budget_tripped"
            if self._interrupted:
                # Pre-flight, mirroring _breaker_tripped() above: a signal
                # caught during setup (before run() was ever called -- see
                # run_walkforward.py's early flag-based handler) must not
                # cost a whole extra step before it's honored (Codex review:
                # "Install signal handlers before initial fitting"). A
                # signal caught MID-step is still only noticed after that
                # step finishes, via the check below.
                self.checkpoint()
                return "interrupted"
            self._steps_taken_in_run += 1
            self.step(date64)
            steps_taken += 1
            if getattr(self, "_model_failure", False):
                self._trip_banner("systemic_model_failure", steps_taken, started_monotonic)
                return "systemic_model_failure"
            if self.step_index % self.config.checkpoint_every == 0:
                self.checkpoint()
            if self._interrupted:
                self.checkpoint()
                return "interrupted"
        self.checkpoint()
        return "completed"

    def _score_one(
        self, entry: dict[str, Any], series, date_iso: str
    ) -> tuple[str, Any]:
        """Score one matured forecast entry against ``series``.

        Returns a ``(tag, payload)`` pair:
        - ``("score", (ledger_record, (ticker, origin_row)))`` -- scored now.
        - ``("defer", (target_date_iso, entry))`` -- the origin row's
          RE-ANCHORED target date is later than ``date_iso``: an archive
          correction moved it past the bucket this entry was filed under
          (e.g. a backfilled historical row shifted the next-trading-day
          alignment). Scoring it now would train every model on a day the
          walk-forward cursor has not reached yet -- lookahead across the
          whole market. The caller must requeue ``entry`` under
          ``self.outstanding[target_date_iso]`` instead of scoring it
          (Codex review: "Defer scores until a revised target date is
          reached").
        - ``("drop", None)`` -- not scoreable at all: the origin day itself
          no longer exists in a revised archive.
        - ``("unresolved", entry)`` -- the origin row still exists but its
          target is temporarily non-finite (an archive correction removed
          its next row, leaving a NaT label_end_date). The caller must
          requeue ``entry`` under ``self.outstanding[UNRESOLVED_KEY]`` so a
          later rebuild that restores the target can still score it
          (Codex review: "Retain forecasts while their revised target is
          unavailable").

        Shared by step()'s per-day scan and rebind_unresolved()'s
        end-of-calendar scoring path.
        """
        # Re-anchor the origin row BY DATE before trusting the stored index:
        # live mode rebuilds the panel daily, and an archive revision that
        # drops or inserts a historical row shifts every later index -- a
        # stale origin_row would then score (and train on!) the wrong day,
        # silently (Antigravity review flagged the narrower out-of-bounds
        # case). Same-date match keeps the fast path; anything else falls
        # back to a lookup or skips.
        origin_row = int(entry["origin_row"])
        # entry["origin_date"] is always a DAY-PRECISION string (every
        # writer formats it via np.datetime_as_string(..., unit="D")), but
        # series.dates preserves whatever time-of-day component the
        # archive's own Date column carries (load_history_csv() does not
        # normalize it). Comparing a day-precision value against a
        # potentially intraday-stamped one with exact equality made both
        # the fast-path check AND the positional fallback search below
        # fail on every row for such an archive -- silently dropping every
        # matured forecast (no score ledgered, no label reaches online
        # training) even though step() reports it as scored (Codex review:
        # "Match forecast origins by calendar day").
        origin64 = np.datetime64(entry["origin_date"])
        if not (0 <= origin_row < len(series.dates)) or series.dates[origin_row].astype("datetime64[D]") != origin64:
            positions = np.flatnonzero(series.dates.astype("datetime64[D]") == origin64)
            if not len(positions):
                return "drop", None  # the origin day no longer exists in a revised archive
            origin_row = int(positions[0])
        # The RECORDED (and now checked) target date is this row's own
        # label_end_date, not the caller's date_iso: a late-rebound score can
        # mature behind today's cursor (a lagging per-ticker archive, or a
        # historical row insertion), and date_iso would then mislabel the
        # regime/market day the score belongs to (Codex review: "Preserve
        # the actual target date for late scores").
        label_end = series.label_end_dates[origin_row]
        target_date_iso = date_iso if np.isnat(label_end) else str(np.datetime_as_string(label_end, unit="D"))
        if target_date_iso > date_iso:
            deferred_entry = dict(entry, origin_row=origin_row)
            return "defer", (target_date_iso, deferred_entry)
        target = series.targets[origin_row]
        if not np.isfinite(target).all():
            # The origin row itself still exists (checked above), but its
            # target is temporarily unavailable -- an archive correction
            # removed the next trading row, leaving label_end_date NaT.
            # Retain it, same as a temporarily-missing ticker, instead of
            # deleting the forecast outright: a later rebuild that restores
            # the row makes this scoreable again (Codex review: "Retain
            # forecasts while their revised target is unavailable").
            return "unresolved", dict(entry, origin_row=origin_row)
        # series.targets is expressed relative to THIS row's CURRENT
        # ref_close, which an archive correction can have changed since the
        # forecast was made and its point/quantiles were fixed relative to
        # entry["ref_close"]. Subtracting log-returns on different
        # reference bases isn't a valid price-error comparison; rebase the
        # realized target onto the STORED reference close first so
        # `predicted - realized` and the price conversion in
        # score_forecast() both operate on the same basis (Codex review:
        # "Score revised bars against the forecast's stored reference").
        #
        # But only when the RAW origin price itself actually changed. A
        # newly-discovered dividend/split instead back-adjusts adj_ratio
        # uniformly across history without touching the raw price --
        # series.targets is already recomputed from the current (correctly
        # rescaled) adjusted prices, and a log-return spanning origin to
        # target is invariant to a uniform rescaling of both ends, so it
        # needs no correction. Applying this rebase anyway (comparing the
        # OLD-vintage adjusted ref_close against the NEW one) double-counts
        # the dividend and scores an accurate forecast as a miss (Codex
        # review: "Rebase against the raw origin price").
        raw_origin_old = entry["ref_close"] / entry["adj_ratio"]
        raw_origin_new = float(series.ref_close[origin_row]) / float(series.adj_ratio[origin_row])
        rebase = (
            0.0
            if np.isclose(raw_origin_old, raw_origin_new, rtol=1e-9)
            else float(np.log(float(series.ref_close[origin_row]) / entry["ref_close"]))
        )
        actual = {component: float(target[position]) + rebase for position, component in enumerate(COMPONENTS)}
        score = score_forecast(
            point=entry["point"],
            quantiles=entry["quantiles"],
            quantiles_raw=entry["quantiles_raw"],
            actual=actual,
            ref_close=entry["ref_close"],
        )
        # Calibrator update deliberately NOT applied here: observe() is one
        # nudge per REVEALED DAY, but _score_one() runs once per ticker, and
        # a full-universe day matures thousands of them -- see
        # observe_batch()'s docstring. Callers accumulate this record's
        # (quantiles, actual) pair and apply one observe_batch() per model
        # after their loop (Codex review: "Aggregate calibration updates
        # once per market day").
        self.running[entry["model"]].add(score)
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "score",
            "model": entry["model"],
            "ticker": entry["ticker"],
            "origin_date": entry["origin_date"],
            "target_date": target_date_iso,
            "seed": self.config.seed,
            "point": entry["point"],
            "quantiles": entry["quantiles"],
            "quantiles_raw": entry["quantiles_raw"],
            "actual": actual,
            "ref_close": entry["ref_close"],
            "adj_ratio": entry["adj_ratio"],
            "score": score,
        }
        return "score", (record, (entry["ticker"], origin_row))

    def _flush_pending_updates(self, context_date_iso: str) -> None:
        """Flush self.pending_revealed through every model's update(), if
        there is anything usable to reveal.

        Shared by step()'s cadence-gated online-update phase and
        rebind_unresolved()'s end-of-calendar recovery pass, which flushes
        unconditionally (ignoring update_every) right before predicting
        from a recovered ticker's newly matured row: skipping this would
        forecast from stale, pre-reveal weights, and unlike a normal
        cadence-skipped day -- which still gets flushed on a later step()
        -- this reveal can sit queued indefinitely if no new calendar date
        arrives for a while (Codex review: "Update models before
        forecasting from recovered rows").
        """
        if not self.pending_revealed:
            return
        revealed_days = []
        still_pending: list[list] = []
        for triple in self.pending_revealed:
            ticker, origin_row, origin_date_iso = triple
            series = self.panel.series.get(ticker)
            if series is None:
                # Temporarily missing from a rebuilt daily panel -- keep it
                # queued for a later update() call instead of losing the
                # online update outright if the ticker returns (Codex
                # review: "Retain queued reveals for temporarily missing
                # tickers").
                still_pending.append(triple)
                continue
            origin_row = int(origin_row)
            # Calendar-day comparison: origin_date_iso is always
            # day-precision, series.dates is now normalized at ingestion
            # but a hand-built panel could still carry an intraday
            # timestamp -- defense in depth (Codex review: "Match queued
            # reveals by calendar day").
            origin64 = np.datetime64(origin_date_iso)
            if (
                not (0 <= origin_row < len(series.dates))
                or series.dates[origin_row].astype("datetime64[D]") != origin64
            ):
                positions = np.flatnonzero(series.dates.astype("datetime64[D]") == origin64)
                if not len(positions):
                    continue  # the origin day no longer exists in a revised archive
                origin_row = int(positions[0])
            # An archive revision between queuing and this flush can remove
            # or invalidate the row that WAS this origin's next trading day
            # -- the origin day itself still exists (so the lookup above
            # found it), but its target is now NaN and its label_end_date
            # NaT. Feeding that straight into make_revealed() would hand
            # every model a NaN residual (Persistence) or a NaT-dated
            # training row (GBM), corrupting online state (Codex review:
            # "Validate queued reveals after rebuilding the panel"). Retain
            # it, same as a temporarily missing ticker, until its target
            # row returns.
            if not np.isfinite(series.targets[origin_row]).all():
                still_pending.append(triple)
                continue
            revealed_days.append(make_revealed(series, origin_row, self.config.window_len))
        if revealed_days:
            # Only call update() when something was actually revealed this
            # round: pending_revealed can be nonempty while every queued
            # entry is temporarily unusable (a missing ticker, a not-yet-
            # finite target after an archive rebuild), and
            # OctopusModel.update([]) still takes gradient steps from its
            # existing recent/replay buffers -- training would then depend
            # on transient archive availability instead of on what was
            # actually revealed (Codex review: "Skip model updates when no
            # reveal is usable").
            for name, model in self.models.items():
                update_stats = model.update(revealed_days, step_index=self.step_index)
                if update_stats.get("refit"):
                    self.log(f"[{context_date_iso}] {name} refit: {update_stats}")
        self.pending_revealed = still_pending

    def _predict_and_ledger(
        self,
        requests: list[ForecastRequest],
        rows_for_requests: list[tuple[str, int]],
        records: list[dict[str, Any]],
        context_date_iso: str,
    ) -> int:
        """Run every model's predict() over ``requests``, ledger + queue the results.

        Shared by step()'s per-day predict phase and rebind_unresolved()'s
        end-of-calendar recovery pass (below) so the review-hardened
        details here -- raw_bands deep-copied before calibration,
        repair_band_geometry applied AFTER calibration, repair counts
        ledgered instead of discarded -- can't silently diverge between
        the two call sites.
        """
        n_predicted = 0
        for name, model in self.models.items():
            forecasts = model.predict(requests)
            if requests and forecasts:
                n_non_finite = sum(
                    1 for f in forecasts
                    if any(not math.isfinite(v) for v in f.point.values())
                )
                if n_non_finite > 0:
                    self.log(
                        f"CRITICAL: Model {name!r} emitted {n_non_finite}/{len(forecasts)} "
                        f"non-finite forecasts on {context_date_iso}!"
                    )
                    self._model_failure = True
            repairs = getattr(model, "last_repairs", None)
            if repairs is not None and (repairs.quantile_crossings or repairs.bar_repairs):
                records.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "record_type": "repair",
                        "model": name,
                        "origin_date": context_date_iso,
                        "target_date": context_date_iso,
                        "seed": self.config.seed,
                        "n_predicted": len(forecasts),
                        **repairs.as_dict(),
                    }
                )
            calibrator = self.calibrators[name]
            for forecast, (ticker, row) in zip(forecasts, rows_for_requests, strict=True):
                label_end = self.panel.series[ticker].label_end_dates[row]
                target_iso = None if np.isnat(label_end) else np.datetime_as_string(label_end, unit="D")
                raw_bands = {component: dict(band) for component, band in forecast.quantiles.items()}
                calibrated = (
                    repair_band_geometry(calibrator.calibrate(forecast)) if self.config.calibration else raw_bands
                )
                entry = {
                    "model": name,
                    "ticker": ticker,
                    "origin_row": row,
                    "origin_date": forecast.origin_date,
                    "point": forecast.point,
                    "quantiles": calibrated,
                    "quantiles_raw": raw_bands,
                    "ref_close": forecast.ref_close,
                    "adj_ratio": forecast.adj_ratio,
                }
                self.outstanding.setdefault(target_iso or UNRESOLVED_KEY, []).append(entry)
                records.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "record_type": "forecast",
                        "target_date": target_iso,
                        "seed": self.config.seed,
                        **{key: value for key, value in entry.items() if key != "origin_row"},
                    }
                )
                n_predicted += 1
        return n_predicted

    def step(self, date64: np.datetime64) -> dict[str, int]:
        """One full reveal->score->diagnose->update->predict day."""
        import pandas as pd

        date_iso = np.datetime_as_string(date64, unit="D")
        records: list[dict[str, Any]] = []

        # -- 1. reveal & score ------------------------------------------------
        # Include any late-rebound forecast whose target fell behind the
        # global cursor.  A per-ticker archive can lag the rest of the market,
        # so looking up only today's exact bucket would strand it forever.
        matured_keys = [key for key in self.outstanding if key and key <= date_iso]
        matured_by_key = [(key, entry) for key in sorted(matured_keys) for entry in self.outstanding.pop(key)]
        matured = [entry for _key, entry in matured_by_key]
        revealed_pairs: set[tuple[str, int]] = set()
        # Per (model, target_date): (quantiles, actual) pairs, applied as
        # one calibrator.observe_batch() call PER DATE below, in
        # chronological order, instead of one observe() per ticker (Codex
        # review: "Aggregate calibration updates once per market day"). A
        # late-rebound backlog can mature several overdue target dates in
        # this single step (matured_keys pops every key <= date_iso, not
        # just today's) -- keying only by model would lump multiple days
        # into one nudge and make catching up two days behave like one
        # (Codex review: "Calibrate each overdue target date separately").
        calibration_batches: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
        for key, entry in matured_by_key:
            series = self.panel.series.get(entry["ticker"])
            if series is None:
                # Temporarily missing from a rebuilt daily panel (CSV gap,
                # unreadable, too-short) -- requeue under the same bucket
                # rather than silently discard: the panel's own non-blocking
                # missing-ticker contract means it may reappear later
                # (Codex review: "Preserve matured forecasts for
                # temporarily missing tickers").
                self.outstanding.setdefault(key, []).append(entry)
                continue
            tag, payload = self._score_one(entry, series, date_iso)
            if tag == "drop":
                continue
            if tag == "unresolved":
                self.outstanding.setdefault(UNRESOLVED_KEY, []).append(payload)
                continue
            if tag == "defer":
                target_date_iso, deferred_entry = payload
                self.outstanding.setdefault(target_date_iso, []).append(deferred_entry)
                continue
            record, pair = payload
            revealed_pairs.add(pair)
            records.append(record)
            calibration_batches.setdefault((record["model"], record["target_date"]), []).append(
                (record["quantiles"], record["actual"])
            )
        for model, target_date in sorted(calibration_batches, key=lambda pair: (pair[1], pair[0])):
            self.calibrators[model].observe_batch(calibration_batches[(model, target_date)], target_date=target_date)

        # -- 2. diagnostics ---------------------------------------------------
        eligible = [
            (ticker, row)
            for ticker, row in self.panel.eligible_on(pd.Timestamp(date64))
            if np.isfinite(self.panel.series[ticker].features[row]).all()
        ]
        diagnostic_pool = eligible
        if len(diagnostic_pool) > self.config.diagnostic_sample:
            rng = np.random.default_rng(derive_seed(self.config.seed, date_iso, "diagnostic"))
            chosen = rng.choice(len(diagnostic_pool), size=self.config.diagnostic_sample, replace=False)
            diagnostic_pool = [diagnostic_pool[index] for index in sorted(chosen)]
        if diagnostic_pool:
            diagnostic_requests = [
                make_request(self.panel.series[ticker], row, self.config.window_len)
                for ticker, row in diagnostic_pool
            ]
            for name, model in self.models.items():
                reconstructions = model.reconstruction_diagnostic(diagnostic_requests)
                if reconstructions is None:
                    continue
                for mode, matrix in reconstructions.items():
                    for position, (ticker, row) in enumerate(diagnostic_pool):
                        actual_bar = self.panel.series[ticker].features[row, _BAR_FEATURE_SLICE]
                        errors = {}
                        for slot, component in enumerate(COMPONENTS):
                            raw_error = float(abs(matrix[position][slot] - actual_bar[slot]))
                            errors[component] = raw_error if np.isfinite(raw_error) else _DIAGNOSTIC_ERROR_BOUND
                        records.append(
                            {
                                "schema_version": SCHEMA_VERSION,
                                "record_type": "diagnostic",
                                "model": name,
                                "ticker": ticker,
                                "origin_date": date_iso,
                                "target_date": date_iso,
                                "seed": self.config.seed,
                                "mode": mode,
                                "abs_err": errors,
                            }
                        )

        # -- 3. online update -------------------------------------------------
        # Release any fresh()-seeded ragged-ticker boundary reveal whose
        # own TARGET date has now actually been reached -- same "pop every
        # key <= today" tolerance step()'s own outstanding-bucket handling
        # already uses for late rebinds, so a seed queued behind several
        # calendar dates (a long gap) still matures correctly instead of
        # requiring an exact-date match (Codex review: "Delay ragged
        # boundary reveals until their label date").
        matured_seed_keys = [key for key in self._pending_seed_reveals if key <= date_iso]
        for key in matured_seed_keys:
            self.pending_revealed.extend(self._pending_seed_reveals.pop(key))
        for ticker, origin_row in sorted(revealed_pairs):
            origin_date_iso = str(np.datetime_as_string(self.panel.series[ticker].dates[origin_row], unit="D"))
            self.pending_revealed.append([ticker, origin_row, origin_date_iso])
        if self.step_index % self.config.update_every == 0:
            self._flush_pending_updates(date_iso)

        # -- 4. predict -------------------------------------------------------
        requests: list[ForecastRequest] = []
        rows_for_requests: list[tuple[str, int]] = []
        for ticker, row in eligible:
            requests.append(make_request(self.panel.series[ticker], row, self.config.window_len))
            rows_for_requests.append((ticker, row))
        n_predicted = 0
        if requests:
            n_predicted = self._predict_and_ledger(requests, rows_for_requests, records, date_iso)

        if records:
            self.ledger.append(records)
        self.cursor += 1
        self.step_index += 1

        primary = self.config.models[0]
        stats = self.running[primary]
        coverage = (stats.in_band_close / stats.n) if stats.n else 0.0
        mae = (stats.abs_close / stats.n) if stats.n else 0.0

        prefix = ""
        total_run = getattr(self, "_total_steps_in_run", None)
        if total_run is not None and total_run > 0:
            cur = getattr(self, "_steps_taken_in_run", self.step_index)
            pct = min(100.0, (cur / total_run) * 100)
            bar_len = 12
            filled = int(bar_len * cur // total_run)
            bar = (
                "=" * filled
                + (">" if filled < bar_len else "")
                + " " * (bar_len - filled - (1 if filled < bar_len else 0))
            )
            prefix = f"[{bar}] {cur}/{total_run} ({pct:3.0f}%) | "

        self.log(
            f"{prefix}[{date_iso}] scored {len(matured)} | predicted {n_predicted} | "
            f"coverage-to-date {coverage:.1%} | close-MAE {mae:.4f} | step {self.step_index}"
        )
        return {"scored": len(matured), "predicted": n_predicted}

    # ------------------------------------------------------------------
    # Live-cycle helpers
    # ------------------------------------------------------------------
    def set_panel(self, panel: TrainingPanel) -> None:
        """Swap in a rebuilt panel (the live daily cycle: new archive day)."""
        self.panel = panel
        for model in self.models.values():
            bind = getattr(model, "bind_panel", None)
            if bind is not None:
                bind(panel)

    def rebind_unresolved(self) -> int:
        """Give unresolved forecasts their target date once the panel knows it."""
        import pandas as pd

        unresolved = self.outstanding.pop(UNRESOLVED_KEY, [])
        still_unresolved: list[dict[str, Any]] = []
        matured_now: list[tuple[Any, dict[str, Any]]] = []
        rebound = 0
        # DATED buckets step()'s own missing-ticker branch re-queues
        # VERBATIM under their unchanged key (the target date was already
        # known -- only the ticker's row was temporarily unavailable that
        # day): a FUTURE step() would naturally re-sweep them (matured_keys
        # pops every key <= that step's date), but once the cursor has no
        # calendar step left THIS invocation, no future step() exists to do
        # that. A ticker's data being restored mid-day -- without the
        # whole-market calendar itself advancing -- left this already-
        # overdue forecast neither scored nor counted, understating
        # daily_update.py's NOOP check and stranding it at least one extra
        # day (Codex review: "Process restored dated forecasts before
        # NOOP"). Drain scoreable ones into the SAME immediate-scoring wave
        # below, exactly like a rebound UNRESOLVED entry with no calendar
        # step remaining already is.
        if self.cursor >= len(self.panel.calendar):
            for key in [key for key in self.outstanding if key and key != UNRESOLVED_KEY]:
                bucket = self.outstanding[key]
                keep: list[dict[str, Any]] = []
                for entry in bucket:
                    series = self.panel.series.get(entry["ticker"])
                    if series is None:
                        keep.append(entry)
                        continue
                    rebound += 1
                    matured_now.append((series, entry))
                if keep:
                    self.outstanding[key] = keep
                else:
                    del self.outstanding[key]
        for entry in unresolved:
            series = self.panel.series.get(entry["ticker"])
            target_iso = None
            if series is not None:
                # Calendar-day comparison: entry["origin_date"] is always
                # day-precision, series.dates is now normalized at
                # ingestion but a hand-built panel could still carry an
                # intraday timestamp -- defense in depth (Codex review:
                # "Rebind live forecasts by calendar day").
                origin64 = np.datetime64(entry["origin_date"])
                positions = np.flatnonzero(series.dates.astype("datetime64[D]") == origin64)
                if not len(positions):
                    # The ticker itself is present, but THIS entry's own
                    # origin row is gone -- an archive correction deleted
                    # or shifted it. Unlike a temporarily-missing ticker
                    # (series is None, handled by falling through to
                    # still_unresolved below), this can NEVER be rebound
                    # against a future rebuild: the row it was anchored to
                    # no longer exists. _score_one() already treats the
                    # identical "origin row missing" case as a drop, not a
                    # keep-forever retain -- without matching that here,
                    # the entry sat in still_unresolved permanently, and
                    # because the TICKER is present
                    # _current_universe_unresolved() kept exporting it
                    # alongside every newer forecast issued for that
                    # ticker, producing duplicate stale rows and inflated
                    # predicted_tickers counts on every daily artifact
                    # (Codex review: "Drop unresolved forecasts whose
                    # origin row was deleted"). Counted into `rebound`
                    # like any other mutation of self.outstanding so the
                    # caller checkpoints the drop.
                    rebound += 1
                    continue
                # The panel may have been rebuilt: re-anchor the row index.
                entry["origin_row"] = int(positions[0])
                label_end = series.label_end_dates[entry["origin_row"]]
                if not np.isnat(label_end):
                    target_iso = np.datetime_as_string(label_end, unit="D")
            if target_iso is None:
                still_unresolved.append(entry)
                continue
            rebound += 1
            if self.cursor < len(self.panel.calendar):
                # If the global calendar has already passed this ticker's
                # newly discovered target, park it on the current cursor so
                # the next step scores it instead of putting it in a bucket
                # that can never be popped.
                cursor_iso = np.datetime_as_string(self.panel.calendar[self.cursor], unit="D")
                self.outstanding.setdefault(max(target_iso, cursor_iso), []).append(entry)
            else:
                # No calendar step remains THIS invocation to ever pop a
                # bucket again -- score it right now instead of stranding it
                # indefinitely (Codex review: "Score rebounds when no
                # calendar step remains").
                matured_now.append((series, entry))
        if still_unresolved:
            self.outstanding[UNRESOLVED_KEY] = still_unresolved
        if matured_now:
            final_date_iso = (
                str(np.datetime_as_string(self.panel.calendar[-1], unit="D")) if len(self.panel.calendar) else ""
            )
            records: list[dict[str, Any]] = []
            # A wave's own recovered-row prediction can itself land on an
            # already-known, already-overdue target: a lagging ticker whose
            # archive backfilled TWO OR MORE missed rows at once has its
            # origin_row + 1 forecast's target (origin_row + 2's date)
            # already resolvable, and since the cursor stays exhausted for
            # this whole call that date is necessarily <= final_date_iso --
            # so it lands in a DATED outstanding bucket, not UNRESOLVED_KEY.
            # No step() will ever pop a dated bucket this invocation, and if
            # the calendar doesn't grow by the next one either,
            # daily_update.py's NOOP shortcut (which only counts pending
            # UNRESOLVED rebinds) can strand it there indefinitely. Loop
            # waves until each ticker's chain actually reaches its
            # currently-unresolved final row instead of stopping after
            # exactly one recovered row (Codex review: "Drain every
            # recovered row behind the cursor").
            wave = matured_now
            while wave:
                # One outstanding entry per MODEL shares the same (ticker,
                # origin_row) pair -- dedupe before queuing, exactly as the
                # normal step() scoring path already does with its
                # revealed_pairs set, or every configured model's online
                # update would absorb the same reveal once per model (Codex
                # review: "Deduplicate reveals scored after the calendar is
                # exhausted").
                revealed_pairs: set[tuple[str, int]] = set()
                # Keyed by (model, target_date), same reasoning as step():
                # several outstanding entries here can carry different REAL
                # target dates (each one's own label_end_date), so one
                # calibrator nudge per model would lump them together
                # (Codex review: "Calibrate each overdue target date
                # separately").
                calibration_batches: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
                for series, entry in wave:
                    tag, payload = self._score_one(entry, series, final_date_iso)
                    if tag == "drop":
                        continue
                    if tag == "unresolved":
                        self.outstanding.setdefault(UNRESOLVED_KEY, []).append(payload)
                        continue
                    if tag == "defer":
                        # Cannot actually happen here: final_date_iso is the
                        # panel's own last calendar day, and a ticker's
                        # label_end_date is always one of ITS OWN rows, which is
                        # necessarily <= the whole-market calendar's last day.
                        # Handled anyway so this path fails closed (requeued,
                        # not silently trained on) rather than crashing if that
                        # invariant is ever violated.
                        target_date_iso, deferred_entry = payload
                        self.outstanding.setdefault(target_date_iso, []).append(deferred_entry)
                        continue
                    record, pair = payload
                    records.append(record)
                    revealed_pairs.add(pair)
                    calibration_batches.setdefault((record["model"], record["target_date"]), []).append(
                        (record["quantiles"], record["actual"])
                    )
                for model, target_date in sorted(calibration_batches, key=lambda pair: (pair[1], pair[0])):
                    self.calibrators[model].observe_batch(
                        calibration_batches[(model, target_date)], target_date=target_date
                    )
                for ticker, origin_row in sorted(revealed_pairs):
                    origin_date_iso = str(
                        np.datetime_as_string(self.panel.series[ticker].dates[origin_row], unit="D")
                    )
                    self.pending_revealed.append([ticker, origin_row, origin_date_iso])
                # Flush BEFORE predicting, unconditionally (not gated by
                # update_every like step()'s own cadence): the recovered row's
                # own reveal was just queued above, and we are about to
                # predict FROM that same ticker's next row. Unlike a normal
                # cadence-skipped step -- which still flushes on a later
                # step() -- this reveal can otherwise sit queued indefinitely
                # if no new calendar date arrives for a while, leaving the
                # emitted forecast built from stale, pre-reveal weights
                # (Codex review: "Update models before forecasting from
                # recovered rows").
                self._flush_pending_updates(final_date_iso)
                # The just-matured row (origin_row + 1, per label_end_date's
                # Date.shift(-1) construction) is now each ticker's newest
                # usable row -- but its OWN date is behind the global cursor,
                # so eligible_on() will never surface it to step()'s predict
                # phase again. Without this, a ticker whose archive update
                # arrives here (rather than through the normal forward walk)
                # gets scored but never gets a following forecast (Codex
                # review: "Predict from ticker rows recovered behind the
                # cursor").
                recovered_requests: list[ForecastRequest] = []
                recovered_rows: list[tuple[str, int]] = []
                for ticker, origin_row in sorted(revealed_pairs):
                    series = self.panel.series.get(ticker)
                    if series is None:
                        continue
                    next_row = origin_row + 1
                    if next_row >= len(series.dates) or not np.isfinite(series.features[next_row]).all():
                        continue
                    recovered_requests.append(make_request(series, next_row, self.config.window_len))
                    recovered_rows.append((ticker, next_row))
                wave = []
                if recovered_requests:
                    self._predict_and_ledger(recovered_requests, recovered_rows, records, final_date_iso)
                    # Pull straight back out of `outstanding` any entry this
                    # predict pass just parked in a DATED bucket (a known,
                    # already-overdue target -- see the loop comment above)
                    # so the next wave keeps draining that ticker's chain
                    # instead of leaving it stranded there.
                    for ticker, row in recovered_rows:
                        series = self.panel.series[ticker]
                        label_end = series.label_end_dates[row]
                        if np.isnat(label_end):
                            continue
                        target_iso = str(np.datetime_as_string(label_end, unit="D"))
                        bucket = self.outstanding.get(target_iso)
                        if not bucket:
                            continue
                        keep: list[dict[str, Any]] = []
                        for candidate in bucket:
                            if candidate["ticker"] == ticker and candidate["origin_row"] == row:
                                wave.append((series, candidate))
                            else:
                                keep.append(candidate)
                        if keep:
                            self.outstanding[target_iso] = keep
                        else:
                            del self.outstanding[target_iso]
            if records:
                self.ledger.append(records)
        # A ticker can join the CURRENT panel's terminal row eligible for
        # its first-ever forecast without any NEW calendar date to carry it
        # there -- a new IPO's backfilled history crossing min_history, a
        # previously-unreadable CSV being restored, or a capped
        # --max-tickers ranking swap admitting a ticker that missed every
        # earlier calendar step while absent. With no future step() left
        # this invocation to ever surface it to eligible_on()'s predict
        # phase, it must be forecasted HERE or never -- otherwise the
        # next-day CSV silently omits it, and once the ticker's actual next
        # row arrives that missed forecast can never be issued
        # retroactively (Codex review: "Treat newly admitted terminal rows
        # as pending work"). Run LAST, after `still_unresolved` has already
        # been written back to self.outstanding[UNRESOLVED_KEY] above: that
        # earlier write is a flat assignment (not an append), so predicting
        # here any sooner would have its freshly-queued entry silently
        # discarded by it. models never change across a harness's lifetime
        # (config_hash freezes them, refusing any resume that alters
        # config.models), so "has ANY outstanding entry" is equivalent to
        # "has one per configured model" -- no per-model partial-coverage
        # case to guard against separately.
        #
        # already_forecast is read from self.outstanding[UNRESOLVED_KEY]'s
        # CURRENT, post-processing state -- NOT the `unresolved` list
        # popped at the top of this method. That original list is stale in
        # BOTH directions: an entry the drop branch above just discarded
        # (its origin row was deleted by an archive correction) is still
        # sitting in `unresolved`, so checking against it wrongly treats
        # that ticker as already covered and skips issuing its replacement
        # forecast forever; conversely a ticker whose forecast just
        # matured via the dated-bucket sweep or the "for entry in
        # unresolved" loop and got a FRESH terminal forecast from the
        # recovered-rows wave above is NOT in the original `unresolved`
        # list (it was never popped from UNRESOLVED_KEY -- it lived in a
        # dated bucket), so checking against that stale list would predict
        # a DUPLICATE forecast for it (Codex review: "Derive terminal
        # coverage from surviving forecasts"). Every ticker's true
        # terminal-row forecast always lands in UNRESOLVED_KEY (never a
        # dated bucket: label_end_date is NaT on the newest row), so this
        # live read is both necessary and sufficient.
        if self.cursor >= len(self.panel.calendar) and len(self.panel.calendar):
            already_forecast = {entry["ticker"] for entry in self.outstanding.get(UNRESOLVED_KEY, [])}
            terminal_date64 = self.panel.calendar[-1]
            terminal_date = pd.Timestamp(terminal_date64)
            candidates: dict[str, int] = dict(self.panel.eligible_on(terminal_date))
            # eligible_on(the market-wide final date) only surfaces a
            # ticker whose OWN terminal row lands on THAT exact date. A
            # ticker whose restored/newly warmed-up CSV is one session
            # behind the rest of the archive has its true final row on an
            # EARLIER date, so it never appears there even though that row
            # is itself eligible and finite -- no later replay step
            # revisits an already-terminal row, so requiring the
            # market-wide final date would drop the ticker's first
            # forecast forever (Codex review: "Forecast newly eligible
            # tickers from their own terminal row").
            for ticker, series in self.panel.series.items():
                if ticker in candidates:
                    continue
                row = len(series.dates) - 1
                if row >= self.panel.min_history and series.dates[row] < terminal_date64:
                    candidates[ticker] = row
            newly_eligible = [
                (ticker, row)
                for ticker, row in candidates.items()
                if ticker not in already_forecast and np.isfinite(self.panel.series[ticker].features[row]).all()
            ]
            # eligible_on() is keyed on the MARKET-WIDE final calendar date, so
            # it only ever returns tickers that traded that exact day. A ticker
            # admitted after the replay had already passed its own last row --
            # a restored CSV, a --max-tickers ranking swap, a newly warmed-up
            # series -- whose archive is even ONE session behind the rest of
            # the market therefore matches nothing here, and with the cursor at
            # the end there is no later step() to reach it either. Its first
            # forecast was lost permanently, and once its next actual row
            # arrives it can never be issued retroactively (Codex review:
            # "Forecast eligible tickers from their own terminal rows"). Same
            # eligibility bar as eligible_on() -- past the warm-up window, all
            # features finite -- applied to each series' OWN terminal row.
            #
            # `covered` includes the rows just selected above, not only
            # already_forecast: a ticker that DID trade on the market-wide
            # terminal date is already handled there, and its own terminal row
            # IS that row, so re-adding it would predict a duplicate.
            covered = already_forecast | {ticker for ticker, _ in newly_eligible}
            for ticker, series in self.panel.series.items():
                if ticker in covered or not len(series):
                    continue
                own_terminal_row = len(series) - 1
                if own_terminal_row < self.panel.min_history:
                    continue
                # >= rather than == : a series dated ON the market-wide
                # terminal date was eligible_on()'s to handle (it either
                # appears above or was excluded there on purpose), and a
                # date beyond the panel calendar cannot occur.
                if pd.Timestamp(series.dates[own_terminal_row]) >= terminal_date:
                    continue
                if not np.isfinite(series.features[own_terminal_row]).all():
                    continue
                newly_eligible.append((ticker, own_terminal_row))
            if newly_eligible:
                requests = [
                    make_request(self.panel.series[ticker], row, self.config.window_len)
                    for ticker, row in newly_eligible
                ]
                new_forecast_records: list[dict[str, Any]] = []
                final_date_iso = str(np.datetime_as_string(self.panel.calendar[-1], unit="D"))
                self._predict_and_ledger(requests, newly_eligible, new_forecast_records, final_date_iso)
                if new_forecast_records:
                    self.ledger.append(new_forecast_records)
                rebound += len(newly_eligible)
        return rebound

    def retrain(self, panel: TrainingPanel, *, cutoff: np.datetime64 | None = None) -> None:
        """Rebuild every model's training state through ``cutoff``.

        The panel normally retains the initial-fit normalization window.  A
        recovery retrain is different: it must explicitly advance that
        window so model ``fit`` implementations rebuild from all information
        revealed at the current walk-forward cursor.
        """
        if cutoff is not None:
            # Local import keeps the harness/model protocol independent from
            # the concrete feature builder during ordinary walk-forward use.
            import features as _features  # type: ignore

            refreeze_fn: Any = getattr(_features, "refreeze_normalization", None)
            if callable(refreeze_fn):
                refreeze_fn(panel, cutoff)
        self.set_panel(panel)
        # Anchor the marker to the last date this harness has actually
        # processed, same convention checkpoint() uses for
        # last_processed_date; falls back to the panel's first calendar date
        # if retrain() is somehow called before any step (cursor == 0).
        if 0 < self.cursor <= len(self.panel.calendar):
            marker_date = str(np.datetime_as_string(self.panel.calendar[self.cursor - 1], unit="D"))
        elif len(self.panel.calendar):
            marker_date = str(np.datetime_as_string(self.panel.calendar[0], unit="D"))
        else:
            marker_date = self.config.start
        records: list[dict[str, Any]] = []
        for name, model in self.models.items():
            # Same per-model wall-clock check fresh()/resume() apply before
            # every model's fit()/load() (Codex review: "Enforce the
            # wall-clock ceiling during model setup", extended to also
            # cover the first model by "Check the wall-clock limit before
            # the first model") -- a recovery retrain re-fits every
            # configured model on FULL history all over again, the same
            # potentially-hours-long cost as the original initial fit, and
            # was the one remaining setup path with no budget check at all
            # (Codex review: "Check the wall-clock budget before recovery
            # refits"). None of self.models' weights are touched until
            # model.fit() below actually runs, so raising here leaves the
            # harness exactly as valid as it was before this retrain() call
            # -- the prior checkpoint on disk is untouched, safe to retry
            # with a larger --max-hours or fewer models.
            if _setup_wallclock_tripped(self.config, self.invocation_started_monotonic):
                raise HarnessError(
                    f"REFUSED: --max-hours budget exhausted during recovery retrain "
                    f"(before refitting {name!r}); increase --max-hours or retrain with fewer models"
                )
            stats = model.fit(
                panel,
                seed=derive_seed(self.config.seed, f"retrain@{self.step_index}", f"fit:{name}"),
                step_index=self.step_index,
            )
            self.log(f"[retrain] {name}: {stats}")
            # A ledgered marker per model lets diagnostic_health() bound its
            # baseline window to dates AFTER the latest retrain instead of
            # forever comparing the recovered encoder against the original
            # run's pre-recovery baseline (Codex review: "Reset encoder-
            # health baseline after recovery retraining").
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "retrain",
                    "model": name,
                    "origin_date": marker_date,
                    "target_date": marker_date,
                    "seed": self.config.seed,
                    "step_index": self.step_index,
                }
            )
        # fit() above produced entirely fresh weights, but every forecast
        # already queued in self.outstanding -- predictions the PRE-retrain
        # models made and ledgered for target dates the cursor has not
        # reached yet -- is untouched by that fit: the live "tomorrow"
        # forecast an operator reads via inspect_run.py --tomorrow (which
        # renders self.outstanding directly, never the ledger), and the
        # value step() eventually scores against the realized bar once its
        # target date arrives, would both keep reflecting whatever the
        # model that PROMPTED this recovery retrain originally guessed,
        # right up until a new day is stepped and produces its own next
        # forecast (Codex review: "Regenerate the live forecast after
        # recovery retraining"). Re-predict every currently-outstanding
        # ticker with the just-fitted models via the same
        # _predict_and_ledger() step() itself uses -- repairs/calibration
        # included -- and replace it in self.outstanding. A ticker missing
        # from THIS panel (a CSV gap/unreadable day) can't be re-predicted;
        # its stale entries are kept rather than dropped, same non-blocking-
        # missing-ticker contract as the pending_revealed handling below.
        old_outstanding = self.outstanding
        self.outstanding = {}
        # One outstanding entry per MODEL shares the same (ticker,
        # origin_date) -- collecting a request per ENTRY (rather than per
        # unique ticker) would queue the same target once per model, and
        # _predict_and_ledger() then runs every model over every duplicate,
        # producing M^2 records for M models instead of M (Codex review:
        # "Deduplicate forecast requests during retraining"; the identical
        # dedup rebind_unresolved() already performs for the same reason).
        # origin_date, not origin_row: origin_row is the checkpoint-era
        # integer index, exactly the kind of raw index a rebuilt panel --
        # the archive correction that likely PROMPTED this recovery retrain
        # in the first place -- can invalidate; re-anchor it against THIS
        # panel by date, the same way rebind_unresolved() already does,
        # rather than trusting it directly (Codex review: "Re-anchor
        # outstanding origins before retraining").
        origin_dates: dict[str, str] = {}
        for key, bucket in old_outstanding.items():
            for entry in bucket:
                ticker = entry["ticker"]
                if ticker in self.panel.series:
                    origin_dates.setdefault(ticker, entry["origin_date"])
                else:
                    self.outstanding.setdefault(key, []).append(entry)
        requests: list[ForecastRequest] = []
        rows_for_requests: list[tuple[str, int]] = []
        for ticker, origin_date_iso in origin_dates.items():
            series = self.panel.series[ticker]
            origin64 = np.datetime64(origin_date_iso)
            positions = np.flatnonzero(series.dates.astype("datetime64[D]") == origin64)
            if not len(positions):
                continue  # the origin row itself no longer exists in the rebuilt archive
            origin_row = int(positions[0])
            requests.append(make_request(series, origin_row, self.config.window_len))
            rows_for_requests.append((ticker, origin_row))
        if requests:
            self._predict_and_ledger(requests, rows_for_requests, records, marker_date)
        self.ledger.append(records)
        # fit() sees everything revealed so far for tickers PRESENT in this
        # panel (the caller re-freezes the normalization/fit window at the
        # cursor before calling retrain()), so a queued reveal is only
        # "baked into" this retrain if its ticker is actually represented
        # here -- unconditionally clearing the whole queue used to also
        # discard a temporarily-missing ticker's (a CSV gap/unreadable day)
        # queued reveal, which contributed NOTHING to this retrain, losing
        # it permanently once the ticker returns and diverging from the
        # normal update() path's deliberate retention of exactly this case
        # (Codex review: "Preserve missing-ticker reveals across recovery
        # retrains"). Retained entries still double-absorb-guard normally:
        # tickers actually represented in the retrain are dropped here so
        # the next update() cadence doesn't re-reveal them.
        self.pending_revealed = [
            triple for triple in self.pending_revealed if triple[0] not in self.panel.series
        ]

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------
    def checkpoint(self) -> None:
        tmp_dir = self.run_dir / "checkpoint.tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        models_dir = tmp_dir / "models"
        models_dir.mkdir(parents=True)
        model_fingerprints: dict[str, dict[str, dict[str, Any]]] = {}
        for name, model in self.models.items():
            verify_fn = getattr(model, "verify_finiteness", None)
            if verify_fn is not None:
                is_finite, err_msg = verify_fn()
                if not is_finite:
                    raise HarnessError(
                        f"REFUSED to write checkpoint: model {name!r} has non-finite state ({err_msg})"
                    )
            model_dir = tmp_dir / "models" / name
            model.save(model_dir)
            # model.save()'s own close()/flush() only reaches the OS page
            # cache, not stable storage -- unlike harness_state.json below,
            # these files were never fsynced before checkpoint.tmp is
            # renamed into place and the previous checkpoint deleted. A
            # crash between those two points could then leave
            # harness_state.json (which IS fsynced) durably recording a
            # model-artifact fingerprint whose bytes never reached disk,
            # and the next resume() refuses with no earlier checkpoint left
            # to fall back to (Codex review: "Sync model artifacts before
            # publishing the checkpoint"). Directory fsyncs too: a file's
            # presence under a directory is itself metadata that needs its
            # own sync on most POSIX filesystems, separate from the file's
            # own data.
            for artifact_path in model_dir.iterdir():
                if artifact_path.is_file():
                    with artifact_path.open("rb") as handle:
                        os.fsync(handle.fileno())
            _fsync_dir(model_dir)
            model_fingerprints[name] = _fingerprint_directory(model_dir)
        _fsync_dir(models_dir)
        last_processed_date = (
            str(np.datetime_as_string(self.panel.calendar[self.cursor - 1], unit="D"))
            if 0 < self.cursor <= len(self.panel.calendar)
            else None
        )
        state = {
            "schema_version": SCHEMA_VERSION,
            "config": self.config.as_dict(),
            "config_hash": self.config.config_hash(),
            "cursor": self.cursor,
            # Date-based cursor anchor: a live-mode resume rebuilds the panel
            # from a GROWN archive, shifting every integer calendar index; the
            # last processed date re-anchors correctly on any calendar.
            "last_processed_date": last_processed_date,
            "step_index": self.step_index,
            "outstanding": self.outstanding,
            "pending_revealed": self.pending_revealed,
            "pending_seed_reveals": self._pending_seed_reveals,
            "calibrators": {name: calibrator.to_state() for name, calibrator in self.calibrators.items()},
            "running": {name: stats.as_dict() for name, stats in self.running.items()},
            "ledger": self.ledger.fingerprints(),
            "model_fingerprints": model_fingerprints,
            "panel_fingerprint": _fingerprint_panel_history(self.panel, last_processed_date),
        }
        state_path = tmp_dir / _STATE_FILENAME
        with state_path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        # tmp_dir's OWN directory entries (harness_state.json plus the
        # models/ subdirectory) were never synced -- only the files/dirs
        # underneath models/ were (the fsync_dir(models_dir) call above).
        # Without this, a crash right after the publish-rename below could
        # rename tmp_dir into place before its own listing is durable,
        # leaving the newly-published checkpoint/ looking incomplete on
        # recovery even though every byte inside it was already synced
        # (Codex review: "Fsync the checkpoint swap before removing the
        # fallback").
        _fsync_dir(tmp_dir)
        checkpoint_dir = self.run_dir / "checkpoint"
        previous_dir = self.run_dir / "checkpoint.prev"
        good_dir = self.run_dir / "checkpoint.good"
        if previous_dir.exists():
            shutil.rmtree(previous_dir)
        if checkpoint_dir.exists():
            checkpoint_dir.rename(previous_dir)
        tmp_dir.rename(checkpoint_dir)
        # Autonomous self-healing: maintain durable checkpoint.good with verified healthy state
        if not _checkpoint_has_nan(checkpoint_dir):
            if good_dir.exists():
                shutil.rmtree(good_dir)
            shutil.copytree(checkpoint_dir, good_dir)
        _fsync_dir(self.run_dir)
        if previous_dir.exists():
            shutil.rmtree(previous_dir)
