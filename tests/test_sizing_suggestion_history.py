"""Tests for sizing_suggestion_history: the append-only per-suggester recommendation log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dca_paths
from sizing_suggestion_history import SCHEMA_VERSION, history_root, iter_history, read_history, record_suggestions

T0 = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)


def test_history_root_follows_cron_tracking_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "elsewhere"))
    assert history_root() == tmp_path / "elsewhere" / "sizing_suggestions"
    assert history_root() == Path(dca_paths.stream_root("sizing_suggestions"))


def test_record_appends_one_line_and_keeps_every_run(tmp_path):
    first = record_suggestions("octopus", {"nvda": 1.3, "AAPL": 1.25}, root=tmp_path, now=T0, run_id="r1")
    second = record_suggestions("octopus", {"AAPL": 1.1}, root=tmp_path, now=T0.replace(day=3), run_id="r2")
    assert first == second == tmp_path / "octopus" / "2026.jsonl"
    rows = read_history("octopus", root=tmp_path)
    assert [r["run_id"] for r in rows] == ["r1", "r2"]  # nothing overwritten: history, not current state
    assert rows[0]["adjustments"] == {"AAPL": 1.25, "NVDA": 1.3}  # upper-cased, sorted
    assert rows[0]["schema_version"] == SCHEMA_VERSION
    assert rows[0]["recorded_at"] == T0.isoformat()


def test_streams_are_separate_per_suggester(tmp_path):
    record_suggestions("octopus", {"AAPL": 1.2}, root=tmp_path, now=T0)
    record_suggestions("ml_forecaster", {"AAPL": 0.9}, root=tmp_path, now=T0)
    assert read_history("octopus", root=tmp_path)[0]["adjustments"] == {"AAPL": 1.2}
    assert read_history("ml_forecaster", root=tmp_path)[0]["adjustments"] == {"AAPL": 0.9}


def test_empty_adjustments_are_still_recorded(tmp_path):
    record_suggestions("octopus", {}, root=tmp_path, now=T0)
    assert read_history("octopus", root=tmp_path)[0]["adjustments"] == {}


def test_year_rolls_to_a_new_file(tmp_path):
    record_suggestions("octopus", {}, root=tmp_path, now=T0)
    record_suggestions("octopus", {}, root=tmp_path, now=T0.replace(year=2027))
    assert sorted(p.name for p in (tmp_path / "octopus").glob("*.jsonl")) == ["2026.jsonl", "2027.jsonl"]
    assert len(read_history("octopus", root=tmp_path)) == 2


@pytest.mark.parametrize("name", ["../x", "Octopus", "a/b", "", "1abc", "a" * 65])
def test_bad_suggester_names_are_refused(tmp_path, name):
    with pytest.raises(ValueError):
        record_suggestions(name, {}, root=tmp_path)
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "1.1", None])
def test_non_finite_or_non_numeric_adjustment_refuses_whole_record(tmp_path, bad):
    with pytest.raises(ValueError):
        record_suggestions("octopus", {"AAPL": 1.1, "NVDA": bad}, root=tmp_path)
    assert not (tmp_path / "octopus").exists()


def test_duplicate_ticker_after_uppercasing_is_refused(tmp_path):
    with pytest.raises(ValueError):
        record_suggestions("octopus", {"aapl": 1.1, "AAPL": 1.2}, root=tmp_path)


def test_nan_in_metrics_is_refused_not_written(tmp_path):
    with pytest.raises(ValueError):
        record_suggestions("octopus", {}, metrics={"mae": float("nan")}, root=tmp_path)
    assert not (tmp_path / "octopus").exists()


def test_torn_or_garbage_lines_are_skipped_not_fatal(tmp_path):
    path = record_suggestions("octopus", {"AAPL": 1.1}, root=tmp_path, now=T0, run_id="ok1")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"torn": \n')
        handle.write("[1, 2]\n")
    record_suggestions("octopus", {"AAPL": 1.2}, root=tmp_path, now=T0, run_id="ok2")
    assert [r["run_id"] for r in iter_history("octopus", root=tmp_path)] == ["ok1", "ok2"]


def test_missing_stream_reads_as_empty(tmp_path):
    assert read_history("octopus", root=tmp_path) == []


def test_lines_are_strict_json(tmp_path):
    path = record_suggestions("octopus", {"AAPL": 1.1}, source={"origin_date": "2026-11-02"}, root=tmp_path, now=T0)
    line = path.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(line, parse_constant=lambda c: pytest.fail(f"non-strict constant {c}"))["source"] == {
        "origin_date": "2026-11-02"
    }


def test_default_root_is_used_when_none_given(tmp_path):
    path = record_suggestions("octopus", {"AAPL": 1.1}, now=T0)
    assert history_root() in path.parents
