#!/usr/bin/env python3
"""Two-stage curriculum pre-training for the octopus over one archive shard.

Stage 1 (*full history foundation*) trains on every trainable window the shard's
tickers have -- 1962 through today -- so the trunk learns regime structure that a
modern-only window never contains (two oil shocks, 1987, the dot-com unwind).
Stage 2 (*modern calibration*) warm-loads those weights and fine-tunes at a low,
constant rate on 2015-onward windows only, so the final checkpoint is anchored in
the regime it will actually forecast.

Relationship to the rest of the tree:

- The multi-task loss is NOT redefined here. Every gradient step goes through
  ``OctopusModel.train_step`` -> ``_train_batch``, which already implements
  ``alpha*forward + beta*recon + gamma*market`` (1.0/0.3/0.1 by default) with the
  non-finite-loss guard, the non-finite-gradient guard, grad clipping and the
  post-step rollback. This module owns only the *curriculum*: which windows, in
  what order, at what learning rate.
- ``run_walkforward.py``/``harness.py`` own walk-forward EVALUATION. This module
  deliberately does not evaluate, ledger, or predict. Its output is a warm-start
  checkpoint plus a metrics artifact, and it never reads or writes ``config/``,
  never trades, and never influences live DCA sizing (docs/wondrous_octopus.md's
  binding scope boundary).
- It writes only under ``--run-dir`` (default ``analysis/foundation-octopus-run``).
  ``analysis/real-octopus-run`` -- the existing walk-forward baseline -- is never
  touched.

Two properties are deliberate and worth knowing before you read the numbers:

1. **Checkpoints here are single ``.pt`` files, not the directory format
   ``OctopusModel.save()`` writes.** That is a divergence from every other
   checkpoint in this tree, taken because the pre-training artifact is a plain
   warm-start payload rather than a resumable harness state (no ledger, no replay
   buffer, no walk-forward cursor). ``checkpoint_payload()`` carries a superset of
   what a warm load needs, so converting one into the directory format later is
   mechanical. A ``.pt`` from here is NOT a drop-in for ``run_walkforward.py
   --resume``.
2. **Normalization is fit over each shard's whole history and then frozen for
   both stages.** That means an 1970 window is scaled by statistics that include
   2026 -- acceptable for representation pre-training, and NOT acceptable as the
   basis for any leakage-free performance claim. Stage 2 reuses Stage 1's vector
   verbatim rather than refitting it, because the Stage 1 ``feat_proj`` weights
   were learned against exactly that scaling; refitting silently corrupts every
   warm-loaded weight while looking like it worked.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

# Mirrors run_sharded.py: the MPS backend still lacks a handful of ops the
# model touches, and the CPU fallback is what makes --device mps usable at all.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

OCTOPUS_DIR = Path(__file__).resolve().parent
REPO_ROOT = OCTOPUS_DIR.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))
if str(OCTOPUS_DIR) not in sys.path:
    sys.path.insert(0, str(OCTOPUS_DIR))

from dca_bootstrap import ensure_venv  # noqa: E402

ensure_venv(
    packages=("numpy", "pandas", "sklearn", "torch"),
    requirements_files=(
        REPO_ROOT / "requirements-dev.txt",
        OCTOPUS_DIR / "requirements.txt",
    ),
)

import features as _features  # noqa: E402

features: Any = _features
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import torch  # noqa: E402  # pyright: ignore[reportMissingImports]
from data import discover_available_tickers  # noqa: E402
from octopus_model import _BAR_SLICE, OctopusModel  # noqa: E402

from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402

#: Bumped whenever the payload's key set changes incompatibly. load_checkpoint()
#: refuses anything it does not recognize rather than warm-loading a partial
#: state dict and training on top of a half-initialized trunk.
CHECKPOINT_FORMAT_VERSION = 1

#: Feature columns the octopus reconstruction head targets. A row is trainable
#: only when these are finite, exactly matching OctopusModel._stream_trainable --
#: duplicated intent, single constant, so the two cannot drift apart.
BAR_SLICE = _BAR_SLICE

DEFAULT_RUN_DIR = REPO_ROOT / "analysis" / "foundation-octopus-run"


@dataclass(frozen=True)
class StageConfig:
    """One curriculum stage: what it sees, for how long, at what rate."""

    stage: int
    epochs: int
    sample_cap: int
    lr_start: float
    lr_end: float
    #: ISO date; windows with an earlier origin are excluded. None = whole archive.
    modern_start: str | None

    @property
    def is_annealed(self) -> bool:
        """True when the rate moves across the run (stage 1), False when flat."""
        return self.lr_start != self.lr_end


#: Stage 1 anneals 3e-4 -> 1e-5 across the WHOLE run (all epochs, not per-epoch):
#: a per-epoch restart would re-heat the trunk right after each pass settled it.
#: Stage 2 is a flat 5e-5 -- an anneal on a 2-3 epoch fine-tune spends most of
#: its steps at a rate too low to move anything.
STAGE_DEFAULTS: dict[int, StageConfig] = {
    1: StageConfig(stage=1, epochs=6, sample_cap=1_000_000, lr_start=3e-4, lr_end=1e-5, modern_start=None),
    2: StageConfig(stage=2, epochs=3, sample_cap=500_000, lr_start=5e-5, lr_end=5e-5, modern_start="2015-01-01"),
}


# ----------------------------------------------------------------------------
# Learning-rate schedule
# ----------------------------------------------------------------------------
def cosine_lr(step: int, total_steps: int, lr_start: float, lr_end: float) -> float:
    """Cosine-annealed learning rate for ``step`` of ``total_steps``.

    ``total_steps`` is the whole stage's step count, not one epoch's, so the
    curve descends once rather than sawtoothing at every epoch boundary. A
    ``total_steps`` of 0 (a stage with nothing to do, or a one-step smoke run)
    returns ``lr_start`` instead of dividing by zero.
    """
    if total_steps <= 0:
        return lr_start
    progress = min(max(step / total_steps, 0.0), 1.0)
    return lr_end + 0.5 * (lr_start - lr_end) * (1.0 + math.cos(math.pi * progress))


# ----------------------------------------------------------------------------
# Window selection -- the curriculum's actual content
# ----------------------------------------------------------------------------
class WindowIndex:
    """Trainable ``(ticker, row)`` windows held in time-major order.

    Three numpy arrays, not a list of tuples: a full-archive shard reaches
    ~6M trainable pairs, and materializing those as Python tuples costs
    several hundred megabytes before a single gradient step runs. Refs are
    built one batch at a time instead (see :meth:`batches`), which keeps the
    resident cost of the index itself around 100 MB for that population.

    Time-major ordering is load-bearing twice over. It is the stated goal
    (preserve temporal structure -- no shuffle), AND it is a throughput win:
    ``_train_batch`` clears the per-day market-embedding cache after every
    optimizer step, so a batch whose rows share a calendar day pays for that
    day's market encoding once instead of once per distinct day in the batch.
    """

    def __init__(self, tickers: list[str], ticker_ids: np.ndarray, rows: np.ndarray, dates: np.ndarray):
        self._tickers = tickers
        self._ticker_ids = ticker_ids
        self._rows = rows
        self._dates = dates

    def __len__(self) -> int:
        return self._ticker_ids.size

    @property
    def date_range(self) -> tuple[str | None, str | None]:
        """(first, last) window origin date as ISO strings, or (None, None)."""
        if not len(self):
            return (None, None)
        as_days = self._dates.astype("datetime64[D]")
        return (str(as_days[0]), str(as_days[-1]))

    def _select(self, keep: np.ndarray) -> "WindowIndex":
        return WindowIndex(self._tickers, self._ticker_ids[keep], self._rows[keep], self._dates[keep])

    def since(self, start: str | None) -> "WindowIndex":
        """Windows whose origin date is on or after ``start``.

        This is how stage 2 restricts itself to 2015-2026. It filters the
        *reference* list, deliberately not the panel: rebuilding the panel from
        2015 would refit the normalization vector, and stage 1's weights were
        learned against the full-history one.
        """
        if start is None:
            return self
        return self._select(self._dates >= np.datetime64(start, "ns"))

    def subsample(self, cap: int | None) -> "WindowIndex":
        """At most ``cap`` windows, strided uniformly across the date range.

        Striding, not head-truncation: capping a 1962-2026 stream by taking the
        first N windows would train the foundation stage on nothing past about
        1975. Order is preserved, so the result is still time-major.
        """
        if cap is None or cap <= 0 or len(self) <= cap:
            return self
        keep = np.unique(np.linspace(0, len(self) - 1, cap).astype(np.int64))
        return self._select(keep)

    def refs(self) -> list[tuple[str, int]]:
        """Every window as ``(ticker, row)``. Test/diagnostic convenience only --
        the training loop uses :meth:`batches` so it never holds them all."""
        return [
            (self._tickers[int(t)], int(r))
            for t, r in zip(self._ticker_ids, self._rows, strict=True)
        ]

    def batches(self, batch_size: int) -> Iterator[list[tuple[str, int]]]:
        """Consecutive, non-overlapping batches in time order."""
        for begin in range(0, len(self), batch_size):
            stop = begin + batch_size
            yield [
                (self._tickers[int(t)], int(r))
                for t, r in zip(self._ticker_ids[begin:stop], self._rows[begin:stop], strict=True)
            ]


def build_window_index(panel: Any) -> WindowIndex:
    """Index every trainable window in ``panel``, sorted by origin date.

    Trainability matches ``OctopusModel._stream_trainable``: finite targets and
    finite bar features. No cutoff is applied -- pre-training is not a
    walk-forward replay, so there is no simulation cursor to respect.
    """
    tickers = sorted(panel.series)
    id_chunks: list[np.ndarray] = []
    row_chunks: list[np.ndarray] = []
    date_chunks: list[np.ndarray] = []
    for ticker_id, ticker in enumerate(tickers):
        series = panel.series[ticker]
        usable = np.isfinite(series.targets).all(axis=1)
        usable &= np.isfinite(series.features[:, BAR_SLICE]).all(axis=1)
        rows = np.flatnonzero(usable)
        if not rows.size:
            continue
        id_chunks.append(np.full(rows.size, ticker_id, dtype=np.int32))
        row_chunks.append(rows.astype(np.int32))
        date_chunks.append(series.dates[rows].astype("datetime64[ns]"))
    if not id_chunks:
        raise ValueError("REFUSED: no trainable windows in this shard's panel")

    ticker_ids = np.concatenate(id_chunks)
    rows_all = np.concatenate(row_chunks)
    dates_all = np.concatenate(date_chunks)
    # Stable sort so a day's tickers keep their alphabetical order -- the
    # resulting batch composition is then reproducible run to run.
    order = np.argsort(dates_all, kind="stable")
    return WindowIndex(tickers, ticker_ids[order], rows_all[order], dates_all[order])


# ----------------------------------------------------------------------------
# Checkpoints -- atomic single-file .pt
# ----------------------------------------------------------------------------
def save_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (tmp -> fsync -> replace).

    A pre-training stage is hours long; a torn write at the end of it would be
    indistinguishable from a good checkpoint until the next warm load failed
    deep inside ``load_state_dict``. The temp file is removed on any failure so
    a previous good checkpoint at ``path`` survives untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(temp_path, "wb") as handle:
            torch.save(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    # fsync the directory too: os.replace() is atomic, but the rename itself is
    # only durable once the parent directory entry reaches disk.
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def load_checkpoint(path: Path) -> dict[str, Any]:
    """Read a checkpoint written by :func:`save_checkpoint`, or refuse.

    ``weights_only=False``: the payload carries the hyper dict, vocabulary and
    normalization vector alongside the tensors, and every file this reads is one
    this tool wrote under its own run directory.
    """
    payload = torch.load(Path(path), map_location="cpu", weights_only=False)
    version = payload.get("format_version")
    if version != CHECKPOINT_FORMAT_VERSION:
        raise ValueError(
            f"REFUSED: checkpoint {path} has format_version {version!r}, "
            f"expected {CHECKPOINT_FORMAT_VERSION}"
        )
    return payload


def checkpoint_payload(
    model: OctopusModel,
    *,
    stage: int,
    shard: int,
    epochs_completed: int,
    feature_names: Sequence[str],
) -> dict[str, Any]:
    """Everything a warm load needs, extracted from a trained model.

    Refuses a non-finite model, mirroring ``OctopusModel.save`` -- a checkpoint
    full of NaNs is worse than no checkpoint, because the next stage inherits it
    silently.
    """
    is_finite, error = model.verify_finiteness()
    if not is_finite:
        raise ValueError(f"REFUSED to save non-finite model: {error}")
    assert model._net is not None and model._optimizer is not None
    return {
        "format_version": CHECKPOINT_FORMAT_VERSION,
        "stage": stage,
        "shard": shard,
        "epochs_completed": epochs_completed,
        "device": model.device,
        "seed": model._seed,
        "window_len": model.hyper["window_len"],
        "hyper": dict(model.hyper),
        "vocab": list(model._vocab),
        "feature_names": tuple(feature_names),
        "normalization": model._normalization,
        "net_state_dict": {key: value.detach().cpu() for key, value in model._net.state_dict().items()},
        "optimizer_state_dict": model._optimizer.state_dict(),
    }


def warm_load(model: OctopusModel, payload: dict[str, Any]) -> None:
    """Restore ``payload``'s weights, optimizer state and normalization into ``model``.

    ``model`` must already have been constructed against a panel with the same
    feature layout and vocabulary; the mismatch checks below turn what would
    otherwise be a confusing ``load_state_dict`` shape error -- or worse, a
    silently mis-scaled network -- into an explicit refusal.
    """
    assert model._net is not None and model._optimizer is not None
    if list(payload["vocab"]) != list(model._vocab):
        raise ValueError(
            "REFUSED: checkpoint vocabulary does not match this panel's "
            f"({len(payload['vocab'])} vs {len(model._vocab)} tickers) -- "
            "stage 2 must run against the same shard as stage 1"
        )
    model._net.load_state_dict(payload["net_state_dict"])
    model._optimizer.load_state_dict(payload["optimizer_state_dict"])
    # The frozen scaling travels WITH the weights. Refitting it from stage 2's
    # narrower date range would leave every feat_proj row expecting the old
    # units -- a silent corruption that looks like a successful warm start.
    model._normalization = payload["normalization"]
    model._market_cache = {}


# ----------------------------------------------------------------------------
# Metrics artifact
# ----------------------------------------------------------------------------
def write_metrics(path: Path, payload: dict[str, Any], *, merge: bool = False) -> None:
    """Write the metrics JSON atomically; ``merge`` folds into an existing file.

    Stage 2 runs after stage 1 has already published its numbers, so the second
    write must not erase the first. ``merge`` deep-merges the ``stages`` map and
    shallow-merges everything else.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    combined: dict[str, Any] = {}
    if merge and path.is_file():
        try:
            combined = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt prior artifact is a reporting problem, never a reason to
            # lose the stage that just finished hours of training.
            combined = {}
    stages = dict(combined.get("stages", {}))
    stages.update(payload.get("stages", {}))
    combined.update({key: value for key, value in payload.items() if key != "stages"})
    if stages:
        combined["stages"] = stages

    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(combined, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def peak_rss_mb() -> float:
    """Peak resident set size of this process, in megabytes.

    ``ru_maxrss`` is bytes on macOS and kilobytes on Linux -- the divisor differs
    by platform, and getting it wrong reports a 1000x-off memory figure.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024**2 if sys.platform == "darwin" else 1024
    return round(peak / divisor, 1)


# ----------------------------------------------------------------------------
# Panel + model construction
# ----------------------------------------------------------------------------
def resolve_shard_tickers(history_dir: Path, shard: int, shards: int) -> list[str]:
    """The 1-indexed shard's ticker list, from the same split run_sharded uses.

    ``run_sharded`` is imported HERE, not at module scope, and deliberately so:
    its module body runs ``os.environ.setdefault("DCA_LEDGER_MAX_LINKS", "2")``
    on darwin, which raises the link count ledger.py tolerates before it
    refuses an aliased shard file. Importing it at module scope leaked that
    setting into every process that so much as imported this module -- including
    pytest collecting tests/test_train_foundation.py, which silently disarmed
    five hardlink-alias refusals in tests/test_ledger_security.py that only fail
    when the whole suite is collected together. This module never touches the
    ledger, so it has no business setting that variable at all.
    """
    from run_sharded import split_into_shards

    available = discover_available_tickers(history_dir)
    groups = split_into_shards(sorted(available), shards)
    if not 1 <= shard <= len(groups):
        raise ValueError(f"REFUSED: shard {shard} outside 1..{len(groups)}")
    return groups[shard - 1]


def build_shard_panel(
    history_dir: Path,
    tickers: Sequence[str],
    *,
    min_history: int = 60,
    end: str | None = None,
    max_tickers: int | None = None,
) -> Any:
    """Assemble the shard's panel over its whole available history.

    ``initial_train_end`` is deliberately left unset, which fits the
    normalization vector over every row rather than a leading window -- see this
    module's docstring for why that is acceptable here and not elsewhere.
    """
    end_ts = pd.Timestamp(end) if end else None
    if end_ts is not None and pd.isna(end_ts):
        end_ts = None
    elif end_ts is not None:
        assert isinstance(end_ts, pd.Timestamp)
    return features.build_panel(
        history_dir,
        tickers=list(tickers),
        max_tickers=max_tickers,
        end=end_ts,
        min_history=min_history,
    )


def initialize_model(panel: Any, *, seed: int, window_len: int, batch_size: int, device: str) -> OctopusModel:
    """Construct and initialize an ``OctopusModel`` bound to ``panel``.

    Runs ``fit()`` with zero epochs: that is the existing, tested path that
    builds the network at the panel's feature width, creates the AdamW
    optimizer, freezes the vocabulary and normalization, and refuses a panel
    with no trainable windows -- all without taking a gradient step, because
    this module drives its own epoch loop. The two reservoirs are pinned to
    capacity 1 so that no-op fit does not also fill a 50k-entry replay buffer
    this pipeline never uses.
    """
    model = OctopusModel(
        window_len=window_len,
        batch_size=batch_size,
        fit_epochs=0,
        fit_sample_cap=1,
        replay_capacity=1,
        device=device,
    )
    model.fit(panel, seed=seed)
    return model


# ----------------------------------------------------------------------------
# The training loop
# ----------------------------------------------------------------------------
def run_stage(
    model: OctopusModel,
    index: WindowIndex,
    config: StageConfig,
    *,
    batch_size: int,
    limit_steps: int | None = None,
    log: Any = print,
) -> dict[str, Any]:
    """Train ``model`` for one curriculum stage and return that stage's metrics.

    Batches are consumed in time order, identically every epoch -- no shuffle.
    The learning rate follows one cosine curve across the stage's whole step
    budget (stage 1) or stays flat (stage 2).
    """
    steps_per_epoch = (len(index) + batch_size - 1) // batch_size
    if limit_steps is not None:
        steps_per_epoch = min(steps_per_epoch, limit_steps)
    total_steps = steps_per_epoch * config.epochs

    first_date, last_date = index.date_range
    epoch_records: list[dict[str, Any]] = []
    global_step = 0
    skipped_steps = 0

    for epoch in range(config.epochs):
        started = time.monotonic()
        epoch_skipped = 0
        totals: list[float] = []
        forwards: list[float] = []
        recons: list[float] = []
        markets: list[float] = []
        epoch_lr_first: float | None = None
        epoch_lr_last: float | None = None

        for step_in_epoch, batch in enumerate(index.batches(batch_size)):
            if limit_steps is not None and step_in_epoch >= limit_steps:
                break
            lr = (
                cosine_lr(global_step, total_steps, config.lr_start, config.lr_end)
                if config.is_annealed
                else config.lr_start
            )
            epoch_lr_first = lr if epoch_lr_first is None else epoch_lr_first
            epoch_lr_last = lr
            total, components = model.train_step(batch, lr=lr)
            global_step += 1
            if not math.isfinite(total):
                # _train_batch already warned and rolled the step back; count it
                # so the metrics artifact shows instability instead of hiding it
                # in an average that silently skipped the bad batches.
                skipped_steps += 1
                epoch_skipped += 1
                continue
            totals.append(total)
            forwards.append(components.get("forward", float("nan")))
            recons.append(components.get("recon", float("nan")))
            markets.append(components.get("market", float("nan")))
            if step_in_epoch and step_in_epoch % 100 == 0:
                log(
                    f"[stage {config.stage}] epoch {epoch + 1}/{config.epochs} "
                    f"step {step_in_epoch}/{steps_per_epoch} lr={lr:.2e} loss={total:.4f}",
                )

        elapsed = time.monotonic() - started
        epoch_records.append(
            {
                "epoch": epoch + 1,
                "seconds": round(elapsed, 2),
                "steps": len(totals) + epoch_skipped,
                "skipped_nonfinite_steps": epoch_skipped,
                "lr_first": epoch_lr_first,
                "lr_last": epoch_lr_last,
                "loss_total_mean": _mean(totals),
                "loss_total_first": totals[0] if totals else None,
                "loss_total_last": totals[-1] if totals else None,
                "loss_forward_mean": _mean(forwards),
                "loss_recon_mean": _mean(recons),
                "loss_market_mean": _mean(markets),
            }
        )
        log(
            f"[stage {config.stage}] epoch {epoch + 1}/{config.epochs} done in {elapsed / 60:.1f} min, "
            f"mean loss {_mean(totals)}"
        )

    is_finite, finiteness_error = model.verify_finiteness()
    return {
        "epochs": epoch_records,
        "epochs_requested": config.epochs,
        "windows": len(index),
        "batch_size": batch_size,
        "steps_per_epoch": steps_per_epoch,
        "total_steps": global_step,
        "skipped_nonfinite_steps": skipped_steps,
        "lr_start": config.lr_start,
        "lr_end": config.lr_end,
        "modern_start": config.modern_start,
        "window_date_first": first_date,
        "window_date_last": last_date,
        "total_seconds": round(sum(record["seconds"] for record in epoch_records), 2),
        "peak_rss_mb": peak_rss_mb(),
        "all_finite": is_finite,
        "finiteness_error": None if is_finite else finiteness_error,
    }


def _mean(values: Sequence[float]) -> float | None:
    """Mean of the finite entries, or None when there are none."""
    finite = [value for value in values if math.isfinite(value)]
    return round(sum(finite) / len(finite), 6) if finite else None


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    """The runner's CLI. Underscore flag spellings carry hyphenated aliases."""
    parser = argparse.ArgumentParser(
        description="Two-stage curriculum pre-training of the octopus over one archive shard.",
    )
    parser.add_argument("--shard", type=int, default=1, help="1-indexed shard to train (default: 1)")
    parser.add_argument("--shards", type=int, default=4, help="Total shards the archive splits into (default: 4)")
    parser.add_argument(
        "--stage",
        choices=("1", "2", "both"),
        default="both",
        help="Curriculum stage to run (default: both, stage 1 then stage 2)",
    )
    parser.add_argument("--epochs", type=int, default=None, help="Override the stage's default epoch count")
    parser.add_argument(
        "--sample_cap",
        "--sample-cap",
        dest="sample_cap",
        type=int,
        default=None,
        help="Override the stage's window cap (1,000,000 stage 1 / 500,000 stage 2)",
    )
    parser.add_argument(
        "--batch_size", "--batch-size", dest="batch_size", type=int, default=256, help="Batch size (default: 256)"
    )
    parser.add_argument(
        "--run-dir", type=Path, default=DEFAULT_RUN_DIR, help=f"Output root (default: {DEFAULT_RUN_DIR})"
    )
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda", "mps"))
    parser.add_argument(
        "--max-tickers", type=int, default=None, help="Keep only the N longest histories in the shard"
    )
    parser.add_argument("--window-len", type=int, default=64, help="Lookback window length (default: 64)")
    parser.add_argument(
        "--modern-start", default="2015-01-01", help="Stage 2's first window date (default: 2015-01-01)"
    )
    parser.add_argument("--end", default=None, help="Last archive date to admit (inclusive)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--limit-steps",
        type=int,
        default=None,
        help="Cap gradient steps per epoch -- for smoke runs and throughput benchmarks, not production",
    )
    return parser


def _stage_config(stage: int, args: argparse.Namespace) -> StageConfig:
    """The stage's defaults with any CLI overrides applied."""
    base = STAGE_DEFAULTS[stage]
    return StageConfig(
        stage=stage,
        epochs=args.epochs if args.epochs is not None else base.epochs,
        sample_cap=args.sample_cap if args.sample_cap is not None else base.sample_cap,
        lr_start=base.lr_start,
        lr_end=base.lr_end,
        modern_start=args.modern_start if base.modern_start is not None else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = run_dir / f"shard_{args.shard}_metrics.json"
    stage1_path = run_dir / f"shard_{args.shard}_stage1.pt"
    final_path = run_dir / f"shard_{args.shard}_final.pt"

    history_dir = configured_ticker_history_dir()
    tickers = resolve_shard_tickers(history_dir, args.shard, args.shards)
    print(f"[panel] shard {args.shard}/{args.shards}: {len(tickers)} tickers from {history_dir}", flush=True)

    panel_started = time.monotonic()
    panel = build_shard_panel(
        history_dir, tickers, end=args.end, max_tickers=args.max_tickers
    )
    panel_seconds = round(time.monotonic() - panel_started, 2)
    full_index = build_window_index(panel)
    print(
        f"[panel] built in {panel_seconds / 60:.1f} min: {len(panel.series)} usable tickers, "
        f"{len(full_index):,} trainable windows, {full_index.date_range[0]} -> {full_index.date_range[1]}, "
        f"peak RSS {peak_rss_mb()} MB",
        flush=True,
    )

    run_stage1 = args.stage in ("1", "both")
    run_stage2 = args.stage in ("2", "both")

    shared = {
        "shard": args.shard,
        "shards": args.shards,
        "tickers_requested": len(tickers),
        "tickers_usable": len(panel.series),
        "panel_build_seconds": panel_seconds,
        "panel_windows_total": len(full_index),
        "torch_version": torch.__version__,
        "feature_count": len(panel.feature_names),
        "window_len": args.window_len,
        "seed": args.seed,
        # Recorded so the artifact is self-describing: a benchmark capped at N
        # steps per epoch must not be mistaken for a completed production run
        # when someone reads the epoch timings months later.
        "limit_steps": args.limit_steps,
        "is_capped_run": args.limit_steps is not None,
    }

    model = initialize_model(
        panel, seed=args.seed, window_len=args.window_len, batch_size=args.batch_size, device=args.device
    )
    shared["device"] = model.device
    assert model._net is not None
    shared["parameters"] = sum(parameter.numel() for parameter in model._net.parameters())
    print(f"[model] {shared['parameters']:,} parameters on {model.device}", flush=True)

    if run_stage1:
        config = _stage_config(1, args)
        index = full_index.subsample(config.sample_cap)
        print(
            f"[stage 1] {len(index):,} windows, {config.epochs} epochs, "
            f"lr {config.lr_start}->{config.lr_end}",
            flush=True,
        )
        metrics = run_stage(model, index, config, batch_size=args.batch_size, limit_steps=args.limit_steps)
        save_checkpoint(
            stage1_path,
            checkpoint_payload(
                model, stage=1, shard=args.shard, epochs_completed=config.epochs,
                feature_names=panel.feature_names,
            ),
        )
        metrics["checkpoint"] = str(stage1_path)
        write_metrics(metrics_path, {**shared, "stages": {"1": metrics}}, merge=True)
        print(f"[stage 1] checkpoint -> {stage1_path}", flush=True)

    if run_stage2:
        if not run_stage1:
            # Warm-load rather than continue in memory: --stage 2 is the resume
            # entry point after a stage-1 run finished in a separate process.
            if not stage1_path.is_file():
                print(f"REFUSED: stage 2 needs {stage1_path}, which does not exist", file=sys.stderr)
                return 1
            warm_load(model, load_checkpoint(stage1_path))
            print(f"[stage 2] warm-loaded {stage1_path}", flush=True)
        config = _stage_config(2, args)
        index = full_index.since(config.modern_start).subsample(config.sample_cap)
        if not len(index):
            print(f"REFUSED: no windows on or after {config.modern_start}", file=sys.stderr)
            return 1
        print(
            f"[stage 2] {len(index):,} windows since {config.modern_start}, "
            f"{config.epochs} epochs, lr {config.lr_start}",
            flush=True,
        )
        metrics = run_stage(model, index, config, batch_size=args.batch_size, limit_steps=args.limit_steps)
        save_checkpoint(
            final_path,
            checkpoint_payload(
                model, stage=2, shard=args.shard, epochs_completed=config.epochs,
                feature_names=panel.feature_names,
            ),
        )
        metrics["checkpoint"] = str(final_path)
        write_metrics(metrics_path, {**shared, "stages": {"2": metrics}}, merge=True)
        print(f"[stage 2] checkpoint -> {final_path}", flush=True)

    print(f"[done] metrics -> {metrics_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
