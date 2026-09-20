"""CLI: the historical walk-forward replay (and its --demo smoke mode).

Simulates time moving forward from ``--start`` (default 2000-01-01, the
first PREDICTION date -- training reaches as far back as each ticker's
archive goes): predict t+1, reveal, score, online-update, advance. Runs are
checkpoint-resumable (`--resume`), recoverable (`--resume --retrain` refits
on all history up to the cursor after major discrepancies or errors), and
report-only re-renderable (`--report-only`).

Exit codes (mirroring the repo's REFUSED-not-traceback CLI convention):
  0  completed (or --dry-run / --demo / --report-only finished)
  1  REFUSED -- invalid arguments or unusable data; nothing was written
  2  runtime failure -- last checkpoint retained, --resume can continue
  3  interrupted (SIGINT/SIGTERM) after a clean checkpoint

This tool never reads or writes the core repo's config/ and never trades;
its outputs are research artifacts under its own run directory. It needs no
model server (no Ollama -- the octopus is a ~1-2M parameter PyTorch model
driven directly by this process); `--device auto` prefers cuda, then Apple
MPS, then CPU.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Enable PyTorch CPU fallback for ops not yet implemented in Apple Silicon MPS backend
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("DCA_LEDGER_MAX_LINKS", "2" if sys.platform == "darwin" else "1")

REPO_ROOT = Path(__file__).resolve().parent.parent
# Index 1, not 0 -- index 0 is this subproject's own directory; see
# ml-forecaster/train.py's identical bootstrap and its comment.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_bootstrap import ensure_venv  # noqa: E402

ensure_venv(
    packages=("numpy", "pandas", "sklearn", "torch"),
    requirements_files=(
        REPO_ROOT / "requirements-dev.txt",
        REPO_ROOT / "wondrous-octopus" / "requirements.txt",
    ),
)

import features as _features  # noqa: E402
import numpy as np  # noqa: E402

from dca_paths import stream_root  # noqa: E402
from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402

features: Any = _features
import metrics as metrics_module  # noqa: E402
from baseline_model import GbmQuantileModel, PersistenceModel, TrainingDataChanged  # noqa: E402
from harness import (  # noqa: E402
    _RESUME_SAFE_FIELDS,
    HarnessConfig,
    HarnessError,
    WalkForwardHarness,
    _acquire_run_lock,
    _fsync_dir,
    _release_run_lock,
)
from ledger import Ledger, LedgerError  # noqa: E402

#: Preset tiers bundling the scale knobs for local machines. Explicit flags
#: always override a preset value (argparse defaults are None sentinels so
#: we can tell "user set it" from "preset should fill it").
PRESETS: dict[str, dict[str, object]] = {
    "quick": {"max_tickers": 500, "window_len": 64, "update_every": 5, "refit_every": 63, "diagnostic_sample": 200},
    "overnight": {
        "max_tickers": 2000, "window_len": 128, "update_every": 2, "refit_every": 21, "diagnostic_sample": 500
    },
    "full": {"max_tickers": None, "window_len": 128, "update_every": 1, "refit_every": 21, "diagnostic_sample": 500},
}

_KNOB_DEFAULTS = {"window_len": 128, "update_every": 1, "refit_every": 21, "diagnostic_sample": 500}

MODEL_CHOICES = ("persistence", "gbm", "octopus", "all")


class Refused(RuntimeError):
    """Argument/data refusal: printed as REFUSED, exit 1, nothing written."""


def _parse_iso_date(value: str) -> str:
    try:
        parsed = np.datetime64(value, "D")
    except ValueError as error:
        raise Refused(f"invalid ISO date {value!r}: {error}") from error
    if np.isnat(parsed):
        # numpy accepts the literal "NaT" as a valid datetime64 without
        # raising; left unchecked it round-trips through HarnessConfig as
        # the STRING "NaT", pd.Timestamp("NaT") turns it into a real NaT,
        # and _fit_normalization's NaT guard then silently falls back to
        # full-history normalization -- a --initial-train-end typo becomes
        # undetected lookahead contamination instead of a refusal (Codex
        # review: "Reject NaT as an ISO date cutoff").
        raise Refused(f"invalid ISO date {value!r}: not a valid calendar date")
    # str(): np.datetime_as_string returns numpy.str_, which pandas 3
    # refuses in Timestamp(); keep plain str at every seam.
    return str(np.datetime_as_string(parsed, unit="D"))


def _parse_now(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise Refused(f"invalid --now value {value!r}: {error}") from error


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive integer")
    return number


def _finite_float(value: str) -> float:
    # argparse's bare `type=float` accepts "nan"/"inf"/"-inf"; multiplied
    # directly into the octopus training objective, a non-finite loss
    # weight makes the first loss and every gradient non-finite, so the run
    # checkpoints corrupted weights and ledgers NaN forecasts instead of
    # refusing the invalid configuration up front (Codex review: "Reject
    # non-finite Octopus loss weights").
    number = float(value)
    if not np.isfinite(number):
        raise argparse.ArgumentTypeError(f"{value!r} must be a finite number")
    return number


def _nonnegative_finite_float(value: str) -> float:
    # Shared by --max-hours (`_breaker_tripped()` evaluates `elapsed_hours
    # >= max_hours_per_run`; a NaN limit makes that comparison always False,
    # silently disabling the advertised hard wall-clock ceiling -- Codex
    # review: "Reject non-finite wall-clock limits") and by --alpha/--beta/
    # --gamma (a negative weight makes gradient descent MAXIMIZE that loss
    # term instead of disabling it -- Codex review: "Reject negative Octopus
    # loss weights"). Both need "finite and >= 0", not merely finite.
    number = _finite_float(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"{value!r} must not be negative")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_walkforward.py",
        description="Wondrous Octopus historical walk-forward replay.",
    )
    parser.add_argument(
        "--history-dir", type=Path, default=None, help="per-ticker CSV archive (default: configured)"
    )
    parser.add_argument("--tickers", nargs="*", default=None, help="explicit tickers (default: whole archive)")
    parser.add_argument("--all-tickers-in-archive", action="store_true", help="explicit opt-in to the full archive")
    parser.add_argument("--max-tickers", type=_positive_int, default=None, help="keep the N longest histories")
    parser.add_argument("--start", default="2000-01-01", help="first PREDICTION date (training reaches further back)")
    parser.add_argument("--end", default=None, help="last simulated date (inclusive)")
    parser.add_argument("--now", default=None, help="as-of replay override: archive rows on/after this day are unseen")
    parser.add_argument(
        "--initial-train-end", default=None, help="initial fit window end (default: day before --start)"
    )
    parser.add_argument("--model", choices=MODEL_CHOICES, default="all")
    parser.add_argument("--preset", choices=sorted(PRESETS), default=None)
    parser.add_argument("--window-len", type=_positive_int, default=None)
    parser.add_argument("--min-history", type=_positive_int, default=60)
    # nonnegative, not merely finite: the value multiplies directly into the
    # training objective, so a negative weight makes gradient descent
    # MAXIMIZE that loss term instead of disabling it (Codex review: "Reject
    # negative Octopus loss weights"); 0 remains the documented way to turn
    # a term off.
    parser.add_argument("--alpha", type=_nonnegative_finite_float, default=1.0, help="forward-loss weight (octopus)")
    parser.add_argument(
        "--beta", type=_nonnegative_finite_float, default=0.3, help="reconstruction-loss weight (octopus)"
    )
    parser.add_argument(
        "--gamma", type=_nonnegative_finite_float, default=0.1, help="market-head loss weight (octopus)"
    )
    parser.add_argument("--online-steps", type=_positive_int, default=4, help="gradient steps per update (octopus)")
    parser.add_argument("--replay-capacity", type=_positive_int, default=50_000)
    parser.add_argument("--update-every", type=_positive_int, default=None)
    parser.add_argument("--refit-every", type=_positive_int, default=None, help="GBM scheduled-refit cadence")
    parser.add_argument("--gbm-row-cap", type=_positive_int, default=500_000)
    parser.add_argument("--diagnostic-sample", type=_positive_int, default=None)
    parser.add_argument("--checkpoint-every", type=_positive_int, default=20)
    parser.add_argument(
        "--max-steps",
        type=_positive_int,
        default=None,
        help="circuit breaker: hard ceiling on simulated days THIS invocation (checkpoint + stop, resume later)",
    )
    parser.add_argument(
        "--max-hours",
        type=_nonnegative_finite_float,
        default=None,
        help="circuit breaker: hard wall-clock budget in hours for THIS invocation",
    )
    parser.add_argument("--no-calibration", action="store_true", help="disable adaptive conformal bands")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--resume", type=Path, default=None, metavar="RUN_DIR")
    parser.add_argument("--retrain", action="store_true", help="with --resume: full-history recovery retrain first")
    parser.add_argument("--report-only", type=Path, default=None, metavar="RUN_DIR")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"), default="auto")
    parser.add_argument("--dry-run", action="store_true", help="validate, load, estimate cost; fit nothing")
    parser.add_argument("--demo", action="store_true", help="self-contained synthetic replay (pre-handoff smoke test)")
    return parser


def _apply_preset(args: argparse.Namespace) -> dict[str, object]:
    """Preset -> knobs, explicit flags winning. Returns the resolved knobs."""
    knobs: dict[str, Any] = dict(_KNOB_DEFAULTS)
    knobs["max_tickers"] = None
    if args.preset:
        knobs.update(PRESETS[args.preset])
    for name in ("window_len", "update_every", "refit_every", "diagnostic_sample", "max_tickers"):
        value = getattr(args, name)
        if value is not None:
            knobs[name] = value
    return knobs


def _model_names(selection: str) -> tuple[str, ...]:
    if selection == "all":
        return ("persistence", "gbm_quantile", "octopus")
    return {"persistence": ("persistence",), "gbm": ("gbm_quantile",), "octopus": ("octopus",)}[selection]


def _build_models(names: tuple[str, ...], args: argparse.Namespace, knobs: dict) -> dict[str, object]:
    models: dict[str, object] = {}
    for name in names:
        if name == "persistence":
            models[name] = PersistenceModel()
        elif name == "gbm_quantile":
            models[name] = GbmQuantileModel(refit_every=int(knobs["refit_every"]), row_cap=args.gbm_row_cap)
        elif name == "octopus":
            # Imported lazily so every non-octopus code path works without
            # torch installed (the ticker-advisor importorskip philosophy).
            from octopus_model import OctopusModel

            models[name] = OctopusModel(
                window_len=int(knobs["window_len"]),
                alpha=args.alpha,
                beta=args.beta,
                gamma=args.gamma,
                online_steps=args.online_steps,
                replay_capacity=args.replay_capacity,
                device=args.device,
            )
    return models


def _model_registry(names: tuple[str, ...]) -> dict[str, object]:
    registry: dict[str, object] = {"persistence": PersistenceModel, "gbm_quantile": GbmQuantileModel}
    if "octopus" in names:
        from octopus_model import OctopusModel

        registry["octopus"] = OctopusModel
    return registry


def _resolve_history_dir(args: argparse.Namespace) -> Path:
    history_dir = args.history_dir if args.history_dir is not None else Path(configured_ticker_history_dir())
    if not history_dir.is_dir():
        raise Refused(
            f"history dir {history_dir} does not exist. Point --history-dir (or $DCA_TICKER_HISTORY_DIR) "
            "at the per-ticker CSV archive, or use --demo for a self-contained smoke run."
        )
    # Anchored to THIS invocation's CWD, not left relative: a relative
    # --history-dir/$DCA_TICKER_HISTORY_DIR persists into run_meta.json
    # verbatim, and daily_update.py's bare `--run-dir` invocation later
    # reconstructs it relative to ITS OWN working directory -- a scheduler
    # launched from elsewhere would then either refuse a healthy archive
    # or, worse, silently read a different same-named directory (Codex
    # review: "Record the archive path independently of the launch
    # directory").
    return history_dir.resolve()


def _default_run_dir(config_hash: str, now: datetime | None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    return Path(stream_root("wondrous_octopus")) / "runs" / f"{stamp}-{config_hash[:8]}"


def _build_config(args: argparse.Namespace, knobs: dict) -> HarnessConfig:
    start = _parse_iso_date(args.start)
    initial_train_end = (
        _parse_iso_date(args.initial_train_end)
        if args.initial_train_end
        else str(np.datetime_as_string(np.datetime64(start) - np.timedelta64(1, "D"), unit="D"))
    )
    if np.datetime64(initial_train_end) >= np.datetime64(start):
        raise Refused("--initial-train-end must be earlier than --start to prevent lookahead")
    end = _parse_iso_date(args.end) if args.end else None
    if end is not None and np.datetime64(end) < np.datetime64(start):
        # Caught here, before the run dir/models/checkpoint exist: an
        # end-before-start panel can contain no prediction date, so letting
        # it through would fit and checkpoint every model before write_report
        # eventually refuses on an empty ledger -- violating the documented
        # "exit 1, nothing written" contract (Codex review: "Reject end
        # dates before the prediction start").
        raise Refused(f"--end {end} is before --start {start}; no prediction date would ever be simulated")
    resolved_models = getattr(args, "_resolved_models", None)
    models = tuple(resolved_models) if resolved_models is not None else _model_names(args.model)
    return HarnessConfig(
        start=start,
        end=end,
        initial_train_end=initial_train_end,
        models=models,
        window_len=int(knobs["window_len"]),
        min_history=args.min_history,
        update_every=int(knobs["update_every"]),
        checkpoint_every=args.checkpoint_every,
        diagnostic_sample=int(knobs["diagnostic_sample"]),
        seed=args.seed,
        calibration=not args.no_calibration,
        max_steps_per_run=args.max_steps,
        max_hours_per_run=args.max_hours,
    )


def _build_panel(
    args: argparse.Namespace,
    knobs: dict,
    config: HarnessConfig,
    history_dir: Path,
    *,
    recorded_tickers: list[str] | None = None,
):
    import pandas as pd

    tickers = recorded_tickers if recorded_tickers is not None else (list(args.tickers) if args.tickers else None)
    if tickers is None and not args.all_tickers_in_archive and args.preset is None and args.max_tickers is None:
        raise Refused(
            "no universe selected: pass --tickers, --all-tickers-in-archive, --max-tickers, or a --preset"
        )
    try:
        panel = features.build_panel(
            history_dir,
            tickers=tickers,
            max_tickers=knobs["max_tickers"],
            as_of=pd.Timestamp(_parse_now(args.now)) if args.now else None,
            end=pd.Timestamp(config.end) if config.end else None,
            initial_train_end=pd.Timestamp(config.initial_train_end) if config.initial_train_end else None,
            min_history=config.min_history,
        )
    except ValueError as error:
        # build_panel() raises plain ValueError for an unusable-archive
        # refusal (message already starts with "REFUSED: no usable
        # tickers"); left uncaught here, it falls through to main()'s
        # generic Exception handler and reports exit 2 "RUNTIME FAILURE"
        # instead of the documented exit 1 "REFUSED" for a data problem --
        # automation would then treat a config/data issue as a retryable
        # crash (Codex review: "Classify unusable archives as refusals").
        raise Refused(str(error)) from error
    if panel.normalization.get("fell_back_to_partial_rows"):
        # Surfaced loudly, not just recorded: this fallback means feature
        # scaling used rows with SOME NaN features (rolling-window warm-up
        # still in progress) because no ticker had a fully-finite row
        # inside the initial-fit window -- legitimate only for tiny
        # archives, and the operator should know. Still bounded by the
        # cutoff -- unlike the earlier full-history fallback, it never
        # reads beyond the initial-train window (Antigravity review:
        # "fallback silent data shift"; Codex review: "Refuse
        # normalization from post-cutoff history").
        print(
            "WARNING: no fully finite feature rows inside the initial-train window; "
            "normalization statistics fell back to rows with partial (NaN-containing) "
            f"features, still bounded by window_end={panel.normalization.get('window_end')}."
        )
    return panel


def write_report(run_dir: Path) -> Path:
    """(Re)build report.json/report.md from the run's ledger. Idempotent."""
    # Bound reads to what the checkpoint fingerprinted: a runtime failure
    # between Ledger.append() and the next checkpoint can leave complete or
    # partial gzip members past the durable boundary (Codex review: "Bound
    # report-only reads to the checkpoint ledger"). _checkpoint_state_path()
    # also falls back to checkpoint.prev/ for the narrower crash window
    # between the two renames of checkpoint()'s swap, where checkpoint/ is
    # briefly absent even though a complete, authoritative checkpoint
    # exists (Codex review: "Bound ledger reads using checkpoint.prev
    # during swap recovery").
    state_path = _checkpoint_state_path(run_dir)
    if state_path is None:
        # Ledger shards can exist before the FIRST checkpoint ever
        # happens (a run appends to the ledger during a step, then
        # crashes before checkpoint()); reading them unrestricted would
        # accept bytes with zero durability authority -- the exact
        # failure the checkpoint-bounded read exists to prevent (Codex
        # review: "Refuse reports when no checkpoint fingerprints
        # exist").
        raise Refused(f"no checkpoint under {run_dir}; ledger bytes without a checkpoint are not proven durable")
    read_fingerprints = json.loads(state_path.read_bytes())["ledger"]
    ledger = Ledger(run_dir, read_fingerprints=read_fingerprints)
    models = ledger.models()
    if not models:
        raise Refused(f"no ledger under {run_dir}")
    universe = None
    meta_path = run_dir / "run_meta.json"
    if meta_path.exists():
        universe = json.loads(meta_path.read_bytes()).get("universe")
    def _make_stream(m: str):
        return lambda: ledger.iter_records(model=m, kind="score")

    streams = {model: _make_stream(model) for model in models}
    diagnostics = _diagnostic_summary(ledger, models)
    report = metrics_module.build_report(streams, universe=universe, diagnostics=diagnostics)  # type: ignore[arg-type]
    report_dir = run_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # render_markdown() includes non-ASCII characters (--, an en dash, a
    # minus sign); an implicit platform-default encoding is not always
    # UTF-8 (Codex review: "Write generated reports using UTF-8").
    (report_dir / "report.md").write_text(metrics_module.render_markdown(report), encoding="utf-8")
    return report_dir / "report.json"


def _diagnostic_summary(ledger: Ledger, models: list[str]) -> dict:
    summary: dict[str, dict] = {}
    for model in models:
        per_mode: dict[str, dict[str, float]] = {}
        for record in ledger.iter_records(model=model, kind="diagnostic"):
            mode = record.get("mode", "time")
            bucket = per_mode.setdefault(mode, {"n": 0.0, "abs_err_close": 0.0})
            bucket["n"] += 1
            bucket["abs_err_close"] += record["abs_err"]["close"]
        if per_mode:
            summary[model] = {
                mode: {"n": int(bucket["n"]), "mae_close": bucket["abs_err_close"] / bucket["n"]}
                for mode, bucket in per_mode.items()
            }
    return summary


def _estimate_cost(
    panel, config: HarnessConfig, names: tuple[str, ...], device: str, *, resume_from: str | None = None
) -> str:
    """A deliberately rough wall-clock estimate from a tiny micro-benchmark.

    resume_from, when given, anchors the step count at the CHECKPOINT's
    already-processed cursor instead of config.start -- a --resume --dry-run
    near the end of a long replay otherwise reports the estimate for almost
    the ENTIRE original replay again, even for a fully caught-up checkpoint
    with nothing left to do (Codex review: "Estimate only the remaining
    resume steps").
    """
    n_days = int(np.searchsorted(panel.calendar, np.datetime64(config.end)) if config.end else len(panel.calendar))
    anchor = resume_from if resume_from is not None else config.start
    start_pos = int(np.searchsorted(panel.calendar, np.datetime64(anchor)))
    steps = max(0, n_days - start_pos)
    avg_daily = max(1, sum(len(rows) for rows in panel.date_index.values()) // max(1, len(panel.calendar)))
    # Micro-benchmark: dense matmul throughput as a proxy for model math.
    size = 512
    matrix = np.random.default_rng(0).normal(size=(size, size)).astype(np.float32)
    began = time.perf_counter()
    for _ in range(10):
        _ = matrix @ matrix
    gemm_seconds = (time.perf_counter() - began) / 10
    per_ticker_step = 2e-6 + gemm_seconds / 5e3  # empirical fudge: features + model math
    per_step = avg_daily * per_ticker_step * len(names)
    if "octopus" in names:
        per_step *= 4  # transformer forward+backward dominates the baselines
        if device == "cpu":
            per_step *= 3
    total = timedelta(seconds=int(steps * per_step))
    return (
        f"rough estimate: ~{steps} steps x ~{avg_daily} tickers/day x {len(names)} model(s) "
        f"=> about {total} on device={device} (micro-benchmark based; treat as +/-3x)"
    )


def _write_demo_archive(directory: Path) -> None:
    """A small, deterministic 3-ticker synthetic archive for --demo."""
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    n = 520  # ~2 synthetic years
    dates = np.arange(np.datetime64("2019-01-01"), np.datetime64("2019-01-01") + np.timedelta64(n * 2, "D"))
    weekdays = dates[(dates.astype("datetime64[D]").view("int64") - 4) % 7 < 5][:n]
    market = rng.normal(0.0003, 0.011, size=n)
    for ticker, beta, seed in (("AAA", 1.0, 1), ("BBB", 0.8, 2), ("CCC", 0.0, 3)):
        ticker_rng = np.random.default_rng(seed)
        returns = ticker_rng.normal(0.0002, 0.015, size=n) + beta * market
        close = 50.0 * np.cumprod(1.0 + returns)
        open_ = np.concatenate([[close[0]], close[:-1]]) * (1.0 + ticker_rng.normal(0, 0.002, size=n))
        high = np.maximum(open_, close) * (1.0 + ticker_rng.uniform(0, 0.008, size=n))
        low = np.minimum(open_, close) * (1.0 - ticker_rng.uniform(0, 0.008, size=n))
        volume = ticker_rng.integers(700_000, 4_000_000, size=n)
        lines = ["Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits"]
        for position in range(n):
            day = np.datetime_as_string(weekdays[position], unit="D")
            lines.append(
                f"{day},{open_[position]:.4f},{high[position]:.4f},{low[position]:.4f},"
                f"{close[position]:.4f},{close[position]:.4f},{volume[position]},0.0,0.0"
            )
        (directory / f"{ticker}.csv").write_text("\n".join(lines) + "\n")


def _install_signal_handlers(harness: WalkForwardHarness) -> None:
    def handler(_signum, _frame):
        harness.request_interrupt()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


class _InterruptFlag:
    """A plain flag SIGINT/SIGTERM can set before any harness exists."""

    def __init__(self) -> None:
        self.requested: bool = False

    def handler(self, _signum, _frame) -> None:
        self.requested = True


def _install_early_signal_handlers() -> _InterruptFlag:
    """Install SIGINT/SIGTERM handlers BEFORE the initial model fit (or a
    resume's model load/refit) -- both are single blocking calls that can
    run for hours. Without a handler installed this early, a SIGTERM kills
    the process immediately (Python's default disposition, no checkpoint
    at all) and a SIGINT raises KeyboardInterrupt, which is not an
    Exception subclass and so is NOT caught by main()'s (Refused,
    HarnessError, LedgerError, TrainingDataChanged) handler -- both lose
    the fit entirely with a raw traceback or a silent kill instead of the
    documented "exit 3: interrupted after a clean checkpoint" contract
    (Codex review: "Install signal handlers before initial fitting").

    A flag (not a not-yet-existing harness) is all that's available this
    early, and fit()/load() are calls this process doesn't control
    internally, so the signal is only actually noticed once that call
    returns -- the current unit of work always finishes, same as
    _breaker_tripped()'s "no grace period, the step after trips" contract,
    just applied to setup instead of a replay step. Once the harness
    exists, _install_signal_handlers() re-points these same two signals at
    it directly, and the caller seeds harness._interrupted from this flag
    so a signal caught during setup is honored on run()'s very first
    iteration rather than lost.
    """
    flag = _InterruptFlag()
    signal.signal(signal.SIGINT, flag.handler)
    signal.signal(signal.SIGTERM, flag.handler)
    return flag


@contextlib.contextmanager
def _restoring_signal_handlers():
    """Hand SIGINT/SIGTERM back to whoever owned them before this call.

    _install_early_signal_handlers() and _install_signal_handlers() both
    replace the process-wide disposition of these two signals, and the
    replacements close over state that only means something for the duration
    of ONE walk-forward invocation: a temporary _InterruptFlag nobody reads
    afterwards, then a completed harness whose request_interrupt() sets a
    field its finished run() loop will never consult again. Leaving them
    installed on the way out silently disarms the host: a long-lived process
    that calls main() twice, or embeds this CLI entry point as a library,
    delivers its NEXT Ctrl-C to that stale state and the signal is swallowed
    instead of interrupting anything (Codex review: "Restore process signal
    handlers after walk-forward runs").

    Restores on EVERY exit path (return, refusal, crash, KeyboardInterrupt),
    which is why it wraps _dispatch() from outside main()'s except clauses
    rather than living inside one of them.
    """
    saved = [(number, signal.getsignal(number)) for number in (signal.SIGINT, signal.SIGTERM)]
    try:
        yield
    finally:
        for number, handler in saved:
            # A handler read back as None means the disposition was set from
            # outside Python (or is otherwise not expressible here); there is
            # nothing meaningful to restore, so leave that signal alone
            # rather than raising a TypeError out of a finally block that is
            # very often already unwinding a real failure.
            if handler is not None:
                signal.signal(number, handler)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    args._provided_options = {token.split("=", 1)[0] for token in raw_argv if token.startswith("--")}
    # _install_early_signal_handlers()/_install_signal_handlers() replace
    # SIGINT/SIGTERM for the duration of the run so a signal can reach the
    # (possibly not-yet-existing) harness instead of Python's default
    # disposition. In a long-lived process that calls main() repeatedly,
    # leaving those handlers installed after this call returns means later
    # signals are forwarded to a harness (or flag) that already finished --
    # effectively ignored -- while the caller's own handlers, and the
    # process's ability to be interrupted normally, never come back
    # (Codex review: "Restore the process signal handlers after the run").
    # Save whatever was installed before this call so every exit path --
    # a normal return or any exception -- restores it. signal.signal() only
    # works in the main thread; an embedded caller invoking main() from a
    # worker thread never gets past _install_early_signal_handlers() (it
    # would already have raised there), so restoring is skipped entirely
    # rather than raising ValueError and masking the real return code on an
    # early-return path that never touched signal state at all (Codex
    # review: "Skip signal restoration when no handlers were installed").
    restore_signal_handlers = threading.current_thread() is threading.main_thread()
    original_sigint = None
    original_sigterm = None
    if restore_signal_handlers:
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        with _restoring_signal_handlers():
            return _dispatch(args)
    except (Refused, HarnessError, LedgerError, TrainingDataChanged, ValueError) as error:
        # GbmQuantileModel.fit() and OctopusModel.fit() raise plain
        # ValueError for a "no trainable window at or before cutoff"
        # refusal (message already starts with "REFUSED"), the same
        # convention build_panel() uses; left unrecognized here, it fell
        # through to the generic handler below and reported exit 2
        # "RUNTIME FAILURE" -- after fresh() had already created the run
        # directory and ledger -- instead of the documented exit 1
        # "REFUSED" for a data/config problem (Codex review: "Route
        # empty-fit refusals through the refusal exit path"). A ValueError
        # WITHOUT that prefix is a genuine bug, not a refusal -- keep
        # treating it as a runtime failure like anything else this
        # boundary didn't expect.
        message = str(error)
        if isinstance(error, ValueError) and not message.startswith("REFUSED"):
            print(f"RUNTIME FAILURE ({error.__class__.__name__}): {error}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            print("The last checkpoint (if any) is retained; --resume can continue the run.", file=sys.stderr)
            return 2
        print(message if message.startswith("REFUSED") else f"REFUSED: {message}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- CLI boundary: report, keep checkpoint, exit 2
        print(f"RUNTIME FAILURE ({error.__class__.__name__}): {error}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("The last checkpoint (if any) is retained; --resume can continue the run.", file=sys.stderr)
        return 2
    finally:
        # Covers every exit path above, including the early returns inside
        # _dispatch() (--report-only, --dry-run) that never install any
        # handler at all -- restoring a signal disposition that was never
        # replaced is a harmless no-op.
        if restore_signal_handlers and original_sigint is not None and original_sigterm is not None:
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)


def _dispatch(args: argparse.Namespace) -> int:
    invocation_started_monotonic = time.monotonic()
    if args.retrain and args.resume is None:
        # --retrain is only ever consulted inside the `args.resume is not
        # None` branch below; without --resume it silently falls through
        # to the fresh-run path and performs an ordinary new fit instead
        # of rejecting the invalid combination. On a whole-market run that
        # typo can consume hours and create an unrelated run directory
        # while the checkpoint the operator meant to recover stays
        # untouched (Codex review: "Refuse --retrain unless a resume
        # target is supplied").
        raise Refused("--retrain requires --resume <run_dir>; it has no effect on a fresh run")
    if args.demo and args.resume is not None:
        # _run_demo() never reads args.resume -- it always calls
        # WalkForwardHarness.fresh() against args.run_dir (a DEFAULT-
        # generated directory when --run-dir is omitted, not the --resume
        # target). A checkpoint already at that default path would refuse
        # via fresh()'s own "already holds a checkpoint" guard, but the
        # common case -- --resume given without a matching --run-dir --
        # silently starts an unrelated fresh demo and exits 0, leaving the
        # checkpoint the operator meant to continue completely untouched
        # (Codex review: "Reject or honor --resume when --demo is set").
        raise Refused("--demo does not support --resume; --demo always starts a fresh synthetic run")
    if args.model in ("octopus", "all") and args.alpha == 0 and args.beta == 0 and args.gamma == 0:
        # Each of --alpha/--beta/--gamma is individually valid at 0 (the
        # documented way to disable ONE loss term), but with all three
        # disabled every training batch backpropagates a zero-weighted
        # objective: no data-derived gradient reaches the randomly
        # initialized network, only optimizer-side effects like AdamW
        # weight decay can move it, and the run is still checkpointed and
        # reported as a trained forecasting experiment (Codex review:
        # "Refuse an Octopus objective with every loss weight disabled").
        raise Refused("--alpha/--beta/--gamma cannot all be 0; the octopus objective would have no gradient")
    if args.report_only is not None:
        path = write_report(args.report_only)
        print(f"report written: {path}")
        return 0

    # Installed unconditionally, before any potentially-hours-long fit or
    # resume-time model load: see _install_early_signal_handlers()'s
    # docstring. Harmless (never consulted) on the --dry-run/--report-only
    # early-return paths above/below.
    interrupt_flag = _install_early_signal_handlers()

    if args.resume is not None:
        _restore_recorded_resume_args(args)
    knobs = _apply_preset(args)

    if args.demo:
        return _run_demo(
            args, knobs, invocation_started_monotonic=invocation_started_monotonic, interrupt_flag=interrupt_flag
        )

    config = _build_config(args, knobs)
    names = config.models
    history_dir = _resolve_history_dir(args)

    if args.resume is not None:
        recorded_tickers = _recorded_resume_tickers(args.resume, args)
        panel = _build_panel(args, knobs, config, history_dir, recorded_tickers=recorded_tickers)
        _verify_resume_not_before_cursor(args.resume, panel)
        # Read-only checks, run BEFORE the dry-run early-return (unlike
        # WalkForwardHarness.resume() itself, just below, which mutates via
        # Ledger.resume() and can trigger a GBM refit-on-load): a resume
        # with an incompatible --window-len, model hyperparameter, or
        # ticker selection used to pass --dry-run's estimate cleanly and
        # then be immediately refused for real, making the documented
        # validation mode unusable to verify a planned continuation (Codex
        # review: "Validate frozen resume settings during dry runs").
        _verify_resume_run_meta_exists(args.resume)
        _verify_resume_universe(args.resume, panel)
        _verify_resume_model_hyper(args.resume, args, knobs)
        _verify_resume_config_hash(args.resume, config)
        if args.dry_run:
            # Must return BEFORE WalkForwardHarness.resume(): Ledger.resume()
            # truncates/deletes shard files and model .load() can trigger a
            # GBM refit, all violating "--dry-run fits/writes nothing" (Codex
            # review: "Honor --dry-run when resuming a replay"). But the
            # checks above (run_meta/universe/hyper/config-hash) are ALL
            # metadata-level -- none of them hash the ledger shards or
            # fingerprint the model artifact files the way resume() itself
            # does via verify_checkpoint_integrity(), so a missing/truncated/
            # tampered checkpointed shard or model file used to print a
            # clean cost estimate here even though the equivalent real
            # --resume immediately refuses (Codex review: "Verify artifacts
            # during replay resume dry runs"). mutate_ledger=False: the same
            # read-only shard/fingerprint pass daily_update.py's own
            # --dry-run uses, never Ledger.resume()'s truncate/delete
            # recovery.
            dry_run_state, _dry_config, _dry_ledger, _dry_checkpoint_dir = (
                WalkForwardHarness.verify_checkpoint_integrity(
                    panel, args.resume, config, strict_model_data=not args.retrain, mutate_ledger=False
                )
            )
            print(f"resume: {len(panel.tickers)} tickers, {len(panel.calendar)} calendar days")
            print(f"models: {', '.join(names)}; window {config.window_len}; start {config.start}")
            print(
                _estimate_cost(
                    panel, config, names, args.device, resume_from=dry_run_state.get("last_processed_date")
                )
            )
            print("dry run: nothing resumed, nothing written")
            return 0
        registry = _model_registry(names)
        harness = WalkForwardHarness.resume(
            panel, registry, args.resume, config,
            invocation_started_monotonic=invocation_started_monotonic,
            strict_model_data=not args.retrain,
            device=args.device,
        )
        # A run that finished at an earlier --end parked its last visible
        # rows' forecasts as unresolved; the extended panel now knows their
        # target dates, so re-key them BEFORE stepping or they would never
        # mature (Codex review: "Rebind unresolved forecasts before an
        # extended replay").
        rebound = harness.rebind_unresolved()
        if rebound:
            print(f"re-bound {rebound} boundary forecast(s) to now-known target dates")
        if args.retrain:
            # Recovery retrain sees everything revealed so far. Pass the
            # last PROCESSED date explicitly so retrain itself rebuilds all
            # model training state against the advanced cutoff.
            cutoff = None
            if harness.cursor > 0:
                cutoff_position = min(harness.cursor, len(panel.calendar)) - 1
                cutoff = panel.calendar[cutoff_position]
            harness.retrain(panel, cutoff=cutoff)
        return _run(harness, args.resume, interrupt_flag=interrupt_flag)

    panel = _build_panel(args, knobs, config, history_dir)
    # A --start later than every visible date (archive/--now cutoff) leaves
    # zero simulation steps; without this check, execution proceeds to fit
    # models and create the ledger/checkpoint/run_meta.json anyway, only for
    # write_report() to refuse afterward -- violating the CLI's own exit-1
    # "nothing written" contract (Codex review: "Refuse starts beyond the
    # visible calendar").
    if len(panel.calendar) == 0 or int(np.searchsorted(panel.calendar, np.datetime64(config.start))) >= len(
        panel.calendar
    ):
        latest = str(np.datetime_as_string(panel.calendar[-1], unit="D")) if len(panel.calendar) else "none"
        raise Refused(f"--start {config.start} is beyond the visible calendar (latest date: {latest})")
    if args.dry_run:
        print(f"universe: {len(panel.tickers)} tickers, {len(panel.calendar)} calendar days")
        print(f"skipped (non-blocking): {len(panel.skipped)}")
        print(f"models: {', '.join(names)}; window {config.window_len}; start {config.start}")
        print(_estimate_cost(panel, config, names, args.device))
        print("dry run: nothing fitted, nothing written")
        return 0

    run_dir = Path(
        args.run_dir if args.run_dir is not None else _default_run_dir(
            config.config_hash(), _parse_now(args.now) if args.now else None
        )
    )
    # Written BEFORE fresh(), not after: fresh() publishes the initial
    # checkpoint (its own expensive initial fit, potentially hours for
    # octopus/GBM at full-universe scale) and REFUSES a second fresh() call
    # once that checkpoint exists, while --resume separately REFUSES
    # without run_meta.json. A process killed between the two writes used
    # to leave a run directory neither a fresh() retry nor a --resume could
    # ever get past, permanently discarding the completed initial fit
    # (Codex review: "Publish run metadata with the initial checkpoint").
    # run_meta.json depends only on args/knobs/panel/history_dir -- all
    # already built above -- so writing it first makes BOTH interruption
    # points recoverable: a crash before this write retries cleanly (no
    # checkpoint yet, fresh() proceeds normally); a crash during fresh()'s
    # own fit still lets a retry's fresh() proceed too, since run_meta.json
    # being present a second time is a harmless overwrite, not a refusal
    # condition.
    #
    # BUT that "harmless overwrite" reasoning breaks down against a run_dir
    # that already holds a VALID checkpoint from a past invocation: writing
    # unconditionally here ran BEFORE fresh()'s own "already holds a
    # checkpoint" refusal check, so even a non-concurrent retry with
    # different flags or a changed archive used to exit REFUSED while
    # leaving run_meta.json rewritten to describe a configuration/universe
    # that was never actually applied to that checkpoint -- corrupting what
    # a later bare --resume or daily_update.py would reconstruct (Codex
    # review: "Acquire the fresh-run lock before replacing metadata"). The
    # lock is acquired HERE, before the write, and fresh()'s own
    # checkpoint-exists guard is duplicated so this function can decide
    # whether writing is safe at all; existing_lock then hands fresh() the
    # SAME already-held lock instead of it trying (and failing) to acquire
    # a second, conflicting one on itself -- the same existing_lock handoff
    # daily_update.py's own NOOP short-circuit uses ahead of resume().
    lock_handle = _acquire_run_lock(run_dir)
    try:
        if (run_dir / "checkpoint").exists() or (run_dir / "checkpoint.prev").exists():
            # fresh() will refuse for this exact reason momentarily; skip
            # touching run_meta.json (not this invocation's run to
            # describe) and skip building models (never used before the
            # refusal raises).
            models = {}
        else:
            run_dir.mkdir(parents=True, exist_ok=True)
            _write_run_meta(run_dir, args, knobs, panel, history_dir)
            models = _build_models(names, args, knobs)
    except BaseException:
        _release_run_lock(lock_handle)
        raise
    # Unwrapped from here on: fresh() now owns lock_handle (releases it on
    # its own failure, transfers it to the harness on success), same as
    # resume() owns the handle daily_update.py's existing_lock hands it.
    harness = WalkForwardHarness.fresh(
        panel, models, run_dir, config,
        invocation_started_monotonic=invocation_started_monotonic,
        existing_lock=lock_handle,
    )
    return _run(harness, run_dir, interrupt_flag=interrupt_flag)


def _tickers_hash(panel) -> str:
    return hashlib.sha256("|".join(panel.tickers).encode("utf-8")).hexdigest()


def _verify_resume_run_meta_exists(run_dir: Path) -> None:
    """A resume without run_meta.json has no universe/model-hyper integrity
    check to run at all: _verify_resume_universe() and
    _verify_resume_model_hyper() below both silently RETURN when the file
    is missing (e.g. the process died between fresh()'s first checkpoint
    and _write_run_meta() running). Those inputs aren't part of
    HarnessConfig, so a resume with a different --tickers/archive/selection
    would then pass the harness's own config-hash check unnoticed and
    continue ledgering a different experiment -- refuse instead of
    silently skipping verification (Codex review: "Refuse replay resumes
    without frozen run metadata").
    """
    if not (Path(run_dir) / "run_meta.json").exists():
        raise Refused(
            f"no run_meta.json under {run_dir}; the recorded universe and model settings are required "
            "to safely verify this resume (restore run_meta.json from a backup, or start a fresh run)"
        )


def _verify_resume_universe(run_dir: Path, panel) -> None:
    """A resume must rebuild the SAME universe -- a silently different ticker
    set would keep the checkpoint valid but change every prediction, breaking
    resume-equals-uninterrupted without any visible error."""
    meta_path = Path(run_dir) / "run_meta.json"
    if not meta_path.exists():
        return
    recorded = json.loads(meta_path.read_bytes()).get("universe", {}).get("tickers_sha256")
    if recorded and recorded != _tickers_hash(panel):
        raise Refused(
            "resumed universe differs from the recorded run (ticker set hash mismatch); "
            "pass the same --tickers/--max-tickers/--preset selection the run was started with"
        )


def _checkpoint_state_path(run_dir: Path) -> Path | None:
    """Locate the harness_state.json a resume should read, honoring the same
    checkpoint.prev recovery WalkForwardHarness.resume() performs.

    checkpoint() writes checkpoint.tmp, renames the current checkpoint/ to
    checkpoint.prev, then renames checkpoint.tmp into checkpoint/. A crash
    between those two renames leaves checkpoint/ absent with the complete,
    authoritative previous checkpoint sitting in checkpoint.prev/ -- reading
    ONLY checkpoint/ here (as the pre-fix code did) would then silently see
    no recorded config at all, so a bare `--resume RUN_DIR` rebuilds the
    request from argparse defaults and gets REFUSED for a config mismatch
    the harness's own recovery would otherwise have sailed through (Codex
    review: "Recover checkpoint metadata before restoring resume
    arguments").
    """
    for candidate in (Path(run_dir) / "checkpoint", Path(run_dir) / "checkpoint.prev"):
        state_path = candidate / "harness_state.json"
        if state_path.exists():
            return state_path
    return None


def _verify_resume_config_hash(run_dir: Path, requested_config: HarnessConfig) -> None:
    """Read-only preview of WalkForwardHarness.resume()'s own config-hash
    check (e.g. a changed --window-len), so --dry-run can report that a
    resume would actually be refused instead of printing a valid-looking
    estimate -- the real resume immediately refuses on exactly this
    mismatch (Codex review: "Validate frozen resume settings during dry
    runs").
    """
    state_path = _checkpoint_state_path(run_dir)
    if state_path is None:
        return
    state = json.loads(state_path.read_bytes())
    config_kwargs: dict[str, Any] = dict(state["config"])
    config_kwargs["models"] = tuple(config_kwargs["models"])
    stored_config = HarnessConfig(**config_kwargs)
    if stored_config.config_hash() == requested_config.config_hash():
        return
    stored = stored_config.as_dict()
    requested = requested_config.as_dict()
    differing = sorted(
        key
        for key in set(stored) | set(requested)
        if key not in _RESUME_SAFE_FIELDS and stored.get(key) != requested.get(key)
    )
    raise Refused(
        f"resumed flags differ from the recorded run config on {differing}; "
        "only resume-safe flags (--end/--now) may change across a resume"
    )


def _verify_resume_not_before_cursor(run_dir: Path, panel) -> None:
    """A resumed panel must not be SHORTER than what the checkpoint already
    processed.

    An earlier --now (or a shrunk archive) truncates the panel; resume()
    would then re-anchor the cursor at the truncated panel's own end, which
    is EARLIER than the checkpoint's last_processed_date, and checkpoint()
    would durably record that earlier date as the new cursor while ledger
    records and model state still reflect the later dates that were already
    processed. A later normal resume would then reprocess those dates and
    append duplicate forecasts/scores (Codex review: "Reject as-of cutoffs
    before the resumed cursor").
    """
    state_path = _checkpoint_state_path(run_dir)
    if state_path is None:
        return
    last_processed = json.loads(state_path.read_bytes()).get("last_processed_date")
    if not last_processed:
        return
    if len(panel.calendar) == 0 or panel.calendar[-1] < np.datetime64(last_processed):
        raise Refused(
            f"resumed panel's latest visible date is before the checkpoint's last processed date "
            f"({last_processed}); pass --now at or after that date, or omit --now/--end to see the "
            "full archive"
        )


def _resume_metadata(run_dir: Path) -> tuple[dict, dict]:
    """Load the frozen CLI metadata and harness config for a resume."""
    meta_path = Path(run_dir) / "run_meta.json"
    state_path = _checkpoint_state_path(run_dir)
    meta = json.loads(meta_path.read_bytes()) if meta_path.exists() else {}
    state = json.loads(state_path.read_bytes()) if state_path is not None else {}
    return meta, state.get("config", {})


def _restore_recorded_resume_args(args: argparse.Namespace) -> None:
    """Make ``--resume RUN_DIR`` reproduce the recorded frozen run spec.

    Explicit options still win, so the existing mismatch checks reject an
    accidental attempted change.  Invocation-safe limits and the end date are
    intentionally not restored: omitting a breaker removes it for this call,
    while an explicit ``--end`` may extend the replay.
    """
    meta, config = _resume_metadata(args.resume)
    provided = args._provided_options
    config_options = {
        "start": "--start",
        "initial_train_end": "--initial-train-end",
        "window_len": "--window-len",
        "min_history": "--min-history",
        "update_every": "--update-every",
        "checkpoint_every": "--checkpoint-every",
        "diagnostic_sample": "--diagnostic-sample",
        "seed": "--seed",
    }
    for attr, option in config_options.items():
        if option not in provided and attr in config:
            setattr(args, attr, config[attr])
    if "--model" not in provided:
        if config.get("models"):
            # Prefer the checkpoint's OWN recorded model tuple over
            # re-deriving one from a --model string: --demo's default run
            # silently narrows "all" to the two torch-free baselines, so
            # replaying "all" through _model_names() on a bare resume would
            # reconstruct three models against a two-model checkpoint and
            # fail the config-hash check (Codex review: "Record the actual
            # model set used by the default demo"). The checkpoint's models
            # list is always the ground truth, demo or not.
            args._resolved_models = tuple(config["models"])
        elif meta.get("model_selection"):
            args.model = meta["model_selection"]
    if "--no-calibration" not in provided and "calibration" in config:
        args.no_calibration = not config["calibration"]
    for attr, value in meta.get("model_hyper", {}).items():
        option = "--" + attr.replace("_", "-")
        if option not in provided:
            setattr(args, attr, value)
    for attr, value in meta.get("knobs", {}).items():
        option = "--" + attr.replace("_", "-")
        if option not in provided:
            setattr(args, attr, value)
    if "--history-dir" not in provided and meta.get("history_dir"):
        args.history_dir = Path(meta["history_dir"])


def _recorded_resume_tickers(run_dir: Path, args: argparse.Namespace) -> list[str] | None:
    """Freeze a resumed replay to its original eligible ticker set."""
    meta, _config = _resume_metadata(run_dir)
    tickers = meta.get("universe", {}).get("tickers")
    # An explicitly different ticker list must still be rebuilt and refused.
    if "--tickers" in args._provided_options or not tickers:
        return None
    return list(tickers)


def _model_hyper(args: argparse.Namespace, knobs: dict) -> dict:
    """The model settings a resume must not silently change (they live in
    model metadata, outside HarnessConfig's hash, so the config-hash check
    alone cannot catch them -- Codex review: "Validate model hyperparameters
    when resuming")."""
    return {
        "alpha": args.alpha,
        "beta": args.beta,
        "gamma": args.gamma,
        "online_steps": args.online_steps,
        "replay_capacity": args.replay_capacity,
        "refit_every": int(knobs["refit_every"]),
        "gbm_row_cap": args.gbm_row_cap,
    }


def _verify_resume_model_hyper(run_dir: Path, args: argparse.Namespace, knobs: dict) -> None:
    meta_path = Path(run_dir) / "run_meta.json"
    if not meta_path.exists():
        return
    recorded = json.loads(meta_path.read_bytes()).get("model_hyper")
    if not recorded:
        return  # run predates hyper recording; nothing to validate against
    requested = _model_hyper(args, knobs)
    differing = sorted(key for key in recorded if recorded[key] != requested.get(key))
    if differing:
        raise Refused(
            f"resumed model settings differ from the recorded run on {differing}; "
            "model hyperparameters are part of the frozen run spec -- pass the original values "
            "(a different configuration is a new run, not a resume)"
        )


def _atomic_write_text(path: Path, content: str) -> None:
    """Write via a same-directory temp file + rename, never a direct write.

    run_meta.json is mandatory for every later --resume/daily_update
    invocation (it holds the frozen ticker selection, archive path, and
    model hyperparameters those verify against, and daily_update.py's
    bare --run-dir invocation reconstructs its args from it). A kill
    between opening the file and finishing a direct write leaves a
    zero-length or truncated JSON there; every later invocation would
    then fail before it could even reach the otherwise-valid checkpoint's
    recovery path (Codex review: "Replace refreshed run metadata
    atomically"). Path.replace is an atomic rename on the same
    filesystem, so readers only ever see the old complete file or the
    new complete file.

    write_text()'s own close() only reaches the OS page cache, not stable
    storage, and the rename's own directory-entry update is buffered
    separately from both -- checkpoint()'s later fsync of run_dir (see its
    own comment) can make THIS rename's directory entry durable without
    the file's DATA being durable, so a crash in between can leave a valid
    checkpoint beside an empty or truncated run_meta.json, and every later
    resume/daily update then refuses (Codex review: "Fsync mandatory run
    metadata before publishing checkpoints"). Fsync the temp file's
    contents before the rename, and the parent directory afterward, the
    same two-part durability checkpoint()'s own publish-rename already
    uses.
    """
    tmp_path = path.parent / f"{path.name}.tmp"
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)
    _fsync_dir(path.parent)


def _write_run_meta(run_dir: Path, args: argparse.Namespace, knobs: dict, panel, history_dir: Path) -> None:
    meta = {
        "history_dir": str(history_dir),
        "universe": {
            "n_tickers": len(panel.tickers),
            "description": (
                f"preset={args.preset}" if args.preset else ("explicit tickers" if args.tickers else "full archive")
            ),
            "max_tickers": knobs["max_tickers"],
            "skipped": len(panel.skipped),
            "tickers_sha256": _tickers_hash(panel),
            "tickers": list(panel.tickers),
        },
        # The selection RULE (not just its hash): daily_update.py re-applies
        # it against tomorrow's grown archive, so new IPOs join the universe
        # exactly the way the original selection would have admitted them.
        "selection": {
            "tickers": list(args.tickers) if args.tickers else None,
            "max_tickers": knobs["max_tickers"],
        },
        "knobs": {key: value for key, value in knobs.items()},
        "model_hyper": _model_hyper(args, knobs),
        "model_selection": args.model,
    }
    _atomic_write_text(Path(run_dir) / "run_meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")


def refresh_run_meta_universe(run_dir: Path, panel) -> None:
    """Update run_meta.json's CURRENT universe snapshot after a panel
    rebuild, leaving the frozen selection RULE (under "selection") and
    everything else untouched.

    The daily cycle re-applies the recorded selection rule against a
    grown archive and admits newly eligible tickers (or swaps members of
    a capped longest-history universe) every invocation; without this,
    write_report() and inspect_run.py's ribbon-ticker fallback would keep
    reading the ORIGINAL fit-time snapshot forever, even though the
    ledger already holds forecasts for the new universe (Codex review:
    "Keep report universe metadata current as tickers join").
    """
    meta_path = Path(run_dir) / "run_meta.json"
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_bytes())
    universe = meta.get("universe")
    if universe is None:
        return
    meta["universe"] = {
        **universe,
        "n_tickers": len(panel.tickers),
        "skipped": len(panel.skipped),
        "tickers_sha256": _tickers_hash(panel),
        "tickers": list(panel.tickers),
    }
    _atomic_write_text(meta_path, json.dumps(meta, indent=2, sort_keys=True) + "\n")


def _run(harness: WalkForwardHarness, run_dir: Path, *, interrupt_flag: _InterruptFlag | None = None) -> int:
    if interrupt_flag is not None and interrupt_flag.requested:
        # A signal arrived during the (now-complete) initial fit/load --
        # honor it on run()'s very first iteration instead of losing it.
        harness.request_interrupt()
    _install_signal_handlers(harness)
    # release_lock=False: write_report() below reads the ledger/checkpoint
    # this run just wrote, and a second process must not begin mutating this
    # run directory while that read is in progress (Codex review: "Hold the
    # run lock through artifact publication"). release_lock() runs in the
    # finally below on every exit path, matching run()'s previous
    # unconditional release semantics for the interrupted/budget_tripped
    # branch (nothing more to publish there) and extending the hold through
    # write_report() for a completed run.
    status = harness.run(release_lock=False)
    try:
        if status == "systemic_model_failure":
            print(
                f"systemic_model_failure: halted run due to non-finite model predictions under {run_dir}. "
                "The last healthy checkpoint is preserved.",
                file=sys.stderr,
            )
            return 2
        if status in {"interrupted", "budget_tripped"}:
            # Both are CLEAN bounded stops with a valid checkpoint -- the
            # circuit-breaker trip already printed its banner.
            print(f"{status}: checkpoint saved under {run_dir}. Resume with --resume {run_dir}")
            return 3
        # The replay loop itself finished cleanly, but report generation --
        # decompressing and intersecting a full-universe, multi-GB score
        # ledger (potentially twice, per write_report()'s own cost) -- is
        # real, unbounded work of its own that the breaker never covers
        # once the loop exits; a replay whose last step lands just under
        # --max-hours can still blow well past the advertised ceiling here
        # even though the checkpoint is already complete and valid (Codex
        # review: "Recheck the wall-clock ceiling before report
        # generation"). Recheck before paying that cost and defer to a
        # later --report-only instead of exceeding the budget to produce
        # it now.
        if harness.config.max_hours_per_run is not None:
            elapsed_hours = (time.monotonic() - harness.invocation_started_monotonic) / 3600.0
            if elapsed_hours >= harness.config.max_hours_per_run:
                print(
                    f"completed: checkpoint saved under {run_dir}; wall-clock budget "
                    f"({harness.config.max_hours_per_run}h) exhausted before report generation -- "
                    f"run --report-only {run_dir} to generate it"
                )
                return 3
        write_report(Path(run_dir))
        print(f"{status}: report under {run_dir}/report/")
        return 0
    finally:
        harness.release_lock()


def _demo_args_and_config(args: argparse.Namespace, knobs: dict) -> tuple[argparse.Namespace, dict, HarnessConfig]:
    demo_args = argparse.Namespace(**vars(args))
    demo_args.history_dir = None  # caller fills in the archive path
    demo_args.tickers = None
    demo_args.all_tickers_in_archive = True
    demo_args.start = "2019-09-01"  # ~40% in: training warm-up before predictions
    demo_args.initial_train_end = None
    demo_args.model = args.model
    demo_knobs = dict(knobs)
    demo_knobs.update({"window_len": 32, "update_every": 2, "refit_every": 63, "diagnostic_sample": 50})
    demo_knobs["max_tickers"] = None
    config = _build_config(demo_args, demo_knobs)
    if args.model == "all":
        # The default demo stays torch-free (baselines only) so the smoke
        # test runs anywhere; --model octopus opts the demo into the torch path.
        config.models = ("persistence", "gbm_quantile")
    return demo_args, demo_knobs, config


def _run_demo(
    args: argparse.Namespace,
    knobs: dict,
    *,
    invocation_started_monotonic: float | None = None,
    interrupt_flag: _InterruptFlag | None = None,
) -> int:
    run_dir = args.run_dir if args.run_dir is not None else _default_run_dir("demo0000", None)
    # Anchored to THIS invocation's CWD, not left relative: the synthetic
    # archive lives at run_dir/demo_archive and that path is persisted into
    # run_meta.json's history_dir verbatim. A later bare `--resume
    # <run_dir>` invocation (not through --demo, which now refuses combined
    # with --resume) restores it and re-resolves against ITS OWN working
    # directory -- a relative run_dir would then read the wrong archive or
    # refuse a healthy one, the same failure _resolve_history_dir() already
    # closed for the non-demo path (Codex review: "Persist the demo archive
    # as an absolute path").
    run_dir = Path(run_dir).resolve()
    if args.dry_run:
        # Must not write under the requested --run-dir: a synthetic archive
        # written before this check, followed by fitting/checkpointing,
        # would break the "fits/writes nothing" promise the same way a
        # resumed dry run did (Codex review: "Honor dry-run before
        # dispatching the demo"). Build the tiny archive in a throwaway
        # temp directory instead.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_archive:
            archive = Path(tmp_archive)
            _write_demo_archive(archive)
            demo_args, demo_knobs, config = _demo_args_and_config(args, knobs)
            demo_args.history_dir = archive
            panel = _build_panel(demo_args, demo_knobs, config, archive)
            names = config.models
            print(f"universe: {len(panel.tickers)} tickers, {len(panel.calendar)} calendar days")
            print(f"models: {', '.join(names)}; window {config.window_len}; start {config.start}")
            print(_estimate_cost(panel, config, names, demo_args.device))
            print("dry run: nothing fitted, nothing written")
        return 0
    archive = run_dir / "demo_archive"
    _write_demo_archive(archive)
    demo_args, demo_knobs, config = _demo_args_and_config(args, knobs)
    demo_args.history_dir = archive
    panel = _build_panel(demo_args, demo_knobs, config, archive)
    models = _build_models(config.models, demo_args, demo_knobs)
    # Keep the demo GBM tiny so the whole smoke test stays in seconds. Both
    # attributes, not just _hist_params: fitting reads _hist_params, but
    # save() persists self.max_iter into metadata.json -- if only
    # _hist_params were updated, a demo interrupted (--max-steps/signal)
    # and later resumed would refit with the ORIGINAL max_iter=100
    # instead of the 20 actually used before the interruption, breaking
    # resume-equals-uninterrupted for the demo path (Codex review:
    # "Persist the reduced demo GBM iteration count").
    for model in models.values():
        if isinstance(model, GbmQuantileModel):
            model._hist_params["max_iter"] = 20
            model.max_iter = 20
    # invocation_started_monotonic: captured in _dispatch() before archive
    # construction and model fitting, same as the non-demo fresh-run path
    # -- omitted here, the harness's own __init__ default (time.monotonic()
    # at CONSTRUCTION time) restarts the clock only after demo setup, so a
    # slow Octopus demo could spend longer than a configured --max-hours
    # budget during setup and still receive the FULL budget again for
    # replay (Codex review: "Carry the wall-clock start into demo
    # harnesses").
    lock_handle = _acquire_run_lock(run_dir)
    try:
        if not ((run_dir / "checkpoint").exists() or (run_dir / "checkpoint.prev").exists()):
            _write_run_meta(run_dir, demo_args, demo_knobs, panel, archive)
    except BaseException:
        _release_run_lock(lock_handle)
        raise
    harness = WalkForwardHarness.fresh(
        panel, models, run_dir, config,
        invocation_started_monotonic=invocation_started_monotonic,
        existing_lock=lock_handle,
    )
    return _run(harness, run_dir, interrupt_flag=interrupt_flag)


if __name__ == "__main__":
    raise SystemExit(main())
