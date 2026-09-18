"""Tests for train_foundation.py -- the two-stage curriculum pre-training runner.

Deliberately fast (no torch model construction, no real archive): the window
stream and the checkpoint payload are both plain data, so the two things worth
regression-testing -- atomic checkpoint save/load round-tripping, and the
window selection that decides WHAT the curriculum sees -- are exercised
against stubs. The heavy end-to-end path (real panel, real gradient steps) is
covered by the operator-run benchmark, not by this suite.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

import train_foundation as tf  # noqa: E402


def _payload(seed: int = 7) -> dict:
    """A checkpoint payload shaped exactly like the real one, but tiny."""
    generator = torch.Generator().manual_seed(seed)
    return {
        "format_version": tf.CHECKPOINT_FORMAT_VERSION,
        "stage": 1,
        "shard": 1,
        "epochs_completed": 2,
        "device": "cpu",
        "seed": seed,
        "window_len": 16,
        "hyper": {"d_model": 16, "alpha": 1.0, "beta": 0.3, "gamma": 0.1},
        "vocab": ["AAA", "BBB"],
        "feature_names": ("ret_close", "realized_vol_20"),
        "normalization": {"mean": [0.0, 1.0], "std": [1.0, 2.0]},
        "net_state_dict": {"feat_proj.weight": torch.rand(4, 2, generator=generator)},
        "optimizer_state_dict": {"state": {}, "param_groups": [{"lr": 3e-4}]},
    }


class TestCheckpointRoundTrip:
    def test_save_then_load_reproduces_every_field(self, tmp_path: Path):
        path = tmp_path / "shard_1_stage1.pt"
        original = _payload()
        tf.save_checkpoint(path, original)

        restored = tf.load_checkpoint(path)

        assert restored["stage"] == original["stage"]
        assert restored["shard"] == original["shard"]
        assert restored["vocab"] == original["vocab"]
        assert restored["normalization"] == original["normalization"]
        assert restored["hyper"] == original["hyper"]
        assert restored["epochs_completed"] == original["epochs_completed"]
        assert torch.equal(
            restored["net_state_dict"]["feat_proj.weight"],
            original["net_state_dict"]["feat_proj.weight"],
        )

    def test_save_leaves_no_temp_file_behind(self, tmp_path: Path):
        path = tmp_path / "shard_1_stage1.pt"
        tf.save_checkpoint(path, _payload())
        assert path.is_file()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_a_failed_save_does_not_corrupt_the_existing_checkpoint(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "shard_1_stage1.pt"
        tf.save_checkpoint(path, _payload(seed=1))
        good = tf.load_checkpoint(path)

        def explode(*_args, **_kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(tf.torch, "save", explode)
        with pytest.raises(RuntimeError):
            tf.save_checkpoint(path, _payload(seed=2))

        # os.replace() never ran, so the previous checkpoint is untouched --
        # a half-written .pt in its place would silently poison a warm-load.
        survivor = tf.load_checkpoint(path)
        assert torch.equal(
            survivor["net_state_dict"]["feat_proj.weight"],
            good["net_state_dict"]["feat_proj.weight"],
        )
        assert list(tmp_path.glob("*.tmp")) == []

    def test_load_refuses_a_checkpoint_from_an_unknown_format_version(self, tmp_path: Path):
        path = tmp_path / "shard_1_stage1.pt"
        payload = _payload()
        payload["format_version"] = tf.CHECKPOINT_FORMAT_VERSION + 99
        tf.save_checkpoint(path, payload)
        with pytest.raises(ValueError, match="REFUSED"):
            tf.load_checkpoint(path)


class _StubSeries:
    """The three TickerSeries attributes the window index actually reads."""

    def __init__(self, dates: list[str], usable: list[bool]):
        self.dates = np.array(dates, dtype="datetime64[ns]")
        finite = np.where(np.array(usable), 0.0, np.nan)
        self.targets = np.repeat(finite[:, None], 4, axis=1).astype(np.float32)
        self.features = np.repeat(finite[:, None], 8, axis=1).astype(np.float32)


class _StubPanel:
    def __init__(self, series: dict[str, _StubSeries]):
        self.series = series


def _stub_panel() -> _StubPanel:
    return _StubPanel(
        {
            # Deliberately out of alphabetical/date alignment so a time-major
            # merge is distinguishable from per-ticker concatenation.
            "BBB": _StubSeries(["1990-01-02", "2016-01-04", "2020-01-06"], [True, True, False]),
            "AAA": _StubSeries(["1962-01-02", "2016-01-05", "2020-01-06"], [True, True, True]),
        }
    )


class TestWindowIndex:
    def test_windows_come_out_in_time_major_order_not_ticker_major(self):
        index = tf.build_window_index(_stub_panel())
        assert index.refs() == [
            ("AAA", 0),  # 1962-01-02
            ("BBB", 0),  # 1990-01-02
            ("BBB", 1),  # 2016-01-04
            ("AAA", 1),  # 2016-01-05
            ("AAA", 2),  # 2020-01-06
        ]

    def test_non_finite_targets_are_excluded(self):
        index = tf.build_window_index(_stub_panel())
        assert ("BBB", 2) not in index.refs()

    def test_since_filters_to_the_modern_era_without_rebuilding_the_panel(self):
        index = tf.build_window_index(_stub_panel()).since("2015-01-01")
        assert index.refs() == [("BBB", 1), ("AAA", 1), ("AAA", 2)]

    def test_sample_cap_strides_across_the_whole_date_range(self):
        # The point of striding rather than head-truncating: a 1962-2026 stream
        # capped at N must still reach the modern era.
        index = tf.build_window_index(_stub_panel()).subsample(2)
        refs = index.refs()
        assert len(refs) == 2
        assert refs[0] == ("AAA", 0)  # oldest kept
        assert refs[-1] == ("AAA", 2)  # newest kept

    def test_sample_cap_above_the_population_is_a_no_op(self):
        full = tf.build_window_index(_stub_panel())
        assert full.subsample(10_000).refs() == full.refs()

    def test_batches_are_contiguous_in_time(self):
        batches = list(tf.build_window_index(_stub_panel()).batches(2))
        assert batches == [[("AAA", 0), ("BBB", 0)], [("BBB", 1), ("AAA", 1)], [("AAA", 2)]]


class TestCosineSchedule:
    def test_anneals_from_start_to_end_across_the_whole_run(self):
        assert tf.cosine_lr(0, 100, 3e-4, 1e-5) == pytest.approx(3e-4)
        assert tf.cosine_lr(100, 100, 3e-4, 1e-5) == pytest.approx(1e-5)
        midpoint = tf.cosine_lr(50, 100, 3e-4, 1e-5)
        assert midpoint == pytest.approx((3e-4 + 1e-5) / 2, rel=1e-6)

    def test_is_monotonically_decreasing(self):
        values = [tf.cosine_lr(step, 50, 3e-4, 1e-5) for step in range(51)]
        assert all(later <= earlier for earlier, later in pairwise(values))

    def test_a_single_step_run_uses_the_starting_rate(self):
        assert tf.cosine_lr(0, 0, 3e-4, 1e-5) == pytest.approx(3e-4)


class TestStageDefaults:
    def test_stage_two_warm_starts_at_a_constant_low_rate(self):
        stage = tf.STAGE_DEFAULTS[2]
        assert stage.lr_start == stage.lr_end == 5e-5
        assert stage.modern_start == "2015-01-01"
        assert stage.sample_cap == 500_000

    def test_stage_one_sees_the_whole_archive(self):
        stage = tf.STAGE_DEFAULTS[1]
        assert stage.modern_start is None
        assert (stage.lr_start, stage.lr_end) == (3e-4, 1e-5)
        assert stage.sample_cap == 1_000_000


class TestMetricsArtifact:
    def test_metrics_are_written_atomically_and_reread(self, tmp_path: Path):
        path = tmp_path / "shard_1_metrics.json"
        tf.write_metrics(path, {"shard": 1, "stages": {"1": {"epochs": []}}})
        assert list(tmp_path.glob("*.tmp")) == []
        import json

        assert json.loads(path.read_text())["shard"] == 1

    def test_rewriting_merges_rather_than_dropping_the_other_stage(self, tmp_path: Path):
        path = tmp_path / "shard_1_metrics.json"
        tf.write_metrics(path, {"shard": 1, "stages": {"1": {"total_seconds": 10}}})
        tf.write_metrics(path, {"stages": {"2": {"total_seconds": 5}}}, merge=True)
        import json

        payload = json.loads(path.read_text())
        assert set(payload["stages"]) == {"1", "2"}
        assert payload["shard"] == 1
