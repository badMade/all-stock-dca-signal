"""Tests for event-driven per-ticker calibration after archive maintenance."""

import json
from datetime import datetime, timezone

from scripts import daily_per_ticker_calibration as calibration


def _ticker_config(path):
    path.write_text(json.dumps({"active": ["ABC"]}), encoding="utf-8")


def test_active_ticker_update_event_triggers_a_calibration(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    artifacts_dir = tmp_path / "artifacts"
    update_event = tmp_path / "ticker_update_event.json"
    _ticker_config(ticker_config)
    (history_dir / "ABC.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2026-08-03,100,101,99,100,100,1000\n",
        encoding="utf-8",
    )
    update_event.write_text(
        json.dumps({"status": "completed", "changed_active_tickers": ["ABC"]}),
        encoding="utf-8",
    )

    result = calibration.rebuild_after_ticker_update(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=output,
        artifacts_dir=artifacts_dir,
        now=datetime(2026, 8, 3, 21, 45, tzinfo=timezone.utc),
        ticker_update_event=update_event,
    )

    assert result["status"] == "completed"
    assert result["execution_owner"] == "claude_only"
    assert result["perplexity_involved"] is False
    assert result["triggering_tickers"] == ["ABC"]
    assert json.loads(output.read_bytes())["tickers"]["ABC"]["sample_sessions"] == 0
    artifact = json.loads((artifacts_dir / "calibration_run.json").read_bytes())
    assert artifact["upstream_status"] == "completed"


def test_no_active_ticker_change_keeps_the_previous_model_unchanged(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    artifacts_dir = tmp_path / "artifacts"
    update_event = tmp_path / "ticker_update_event.json"
    _ticker_config(ticker_config)
    output.write_text('{"status": "existing"}\n', encoding="utf-8")
    update_event.write_text(
        json.dumps({"status": "completed", "changed_active_tickers": []}), encoding="utf-8"
    )
    result = calibration.rebuild_after_ticker_update(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=output,
        artifacts_dir=artifacts_dir,
        now=datetime(2026, 8, 3, 21, 45, tzinfo=timezone.utc),
        ticker_update_event=update_event,
    )

    assert result["status"] == "skipped_no_active_ticker_change"
    assert output.read_text(encoding="utf-8") == '{"status": "existing"}\n'


def test_standalone_recalculation_does_not_require_an_update_event(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    artifacts_dir = tmp_path / "artifacts"
    _ticker_config(ticker_config)
    (history_dir / "ABC.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2026-08-03,100,101,99,100,100,1000\n",
        encoding="utf-8",
    )

    result = calibration.rebuild_after_ticker_update(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=output,
        artifacts_dir=artifacts_dir,
        now=datetime(2026, 8, 3, 22, 30, tzinfo=timezone.utc),
        ticker_update_event=tmp_path / "not-required.json",
        force=True,
    )

    assert result["status"] == "completed"
    assert result["trigger_source"] == "standalone"
    assert output.is_file()


def test_standalone_run_refuses_to_publish_when_the_archive_is_empty(tmp_path):
    # Codex review ("Refuse to publish when the history archive is
    # unavailable"): a missing, unmounted, or empty ticker_data archive makes
    # every active ticker look like it has insufficient history, which would
    # otherwise atomically replace the live model with global fallback tiers
    # for the WHOLE active universe. The scheduled standalone run must
    # refuse to publish and keep the previous calibration instead.
    history_dir = tmp_path / "histories"
    history_dir.mkdir()  # present but empty -- e.g. an unmounted archive link
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    artifacts_dir = tmp_path / "artifacts"
    _ticker_config(ticker_config)
    output.write_text('{"status": "existing"}\n', encoding="utf-8")

    result = calibration.rebuild_after_ticker_update(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=output,
        artifacts_dir=artifacts_dir,
        now=datetime(2026, 8, 3, 22, 30, tzinfo=timezone.utc),
        ticker_update_event=tmp_path / "not-required.json",
        force=True,
    )

    assert result["status"] == "blocked_archive_unavailable"
    assert result["archive_unavailable"]["missing_history_tickers"] == ["ABC"]
    assert output.read_text(encoding="utf-8") == '{"status": "existing"}\n'


def test_standalone_run_refuses_to_publish_a_truncated_ticker_history(tmp_path):
    # Codex review ("Reject the truncated FLKR calibration"): the daily
    # updater's contract is to merge in recent rows without truncating older
    # ones, so a real history file only ever grows. A ticker whose usable
    # session count collapses from a healthy prior calibration is evidence of
    # a truncated or malformed archive merge, not a real drop in history --
    # publishing it would switch that ticker's live thresholds from its
    # historical percentile ladder to the global fallback ladder.
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    artifacts_dir = tmp_path / "artifacts"
    _ticker_config(ticker_config)
    output.write_text(
        json.dumps({"tickers": {"ABC": {"sample_sessions": 300, "source": "three_year_adjusted_daily_low_percentiles"}}}),
        encoding="utf-8",
    )
    # Only 2 rows -> 1 usable drop, far below the 150-session regression floor.
    (history_dir / "ABC.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2026-08-02,100,101,99,100,100,1000\n"
        "2026-08-03,100,101,99,100,100,1000\n",
        encoding="utf-8",
    )

    result = calibration.rebuild_after_ticker_update(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=output,
        artifacts_dir=artifacts_dir,
        now=datetime(2026, 8, 3, 22, 30, tzinfo=timezone.utc),
        ticker_update_event=tmp_path / "not-required.json",
        force=True,
    )

    assert result["status"] == "blocked_session_count_regression"
    assert result["session_count_regressions"]["ABC"]["previous"] == 300
    published = json.loads(output.read_bytes())
    assert published["tickers"]["ABC"]["sample_sessions"] == 300  # unchanged live config


def test_session_count_regression_bound_catches_a_moderate_collapse(tmp_path):
    # Codex review ("Reject materially smaller calibration samples"): a
    # tolerance anywhere near 50% would let 751 sessions collapse to 376 (a
    # real incident's shape) and still publish. The rolling three-year
    # window only advances by about a day of sessions per nightly run, so a
    # healthy recalculation should never lose anywhere close to half its
    # sample -- the bound must be tight enough to catch that collapse, not
    # just a near-total (164-session) one.
    output = tmp_path / "per_ticker_sizing.json"
    output.write_text(
        json.dumps({"tickers": {"FLKR": {"sample_sessions": 751}}}), encoding="utf-8"
    )

    regressions = calibration._session_count_regressions({"FLKR": {"sample_sessions": 376}}, output)

    assert regressions == {"FLKR": {"previous": 751, "new": 376}}


def test_session_count_regression_bound_tolerates_normal_daily_movement(tmp_path):
    output = tmp_path / "per_ticker_sizing.json"
    output.write_text(
        json.dumps({"tickers": {"FLKR": {"sample_sessions": 751}}}), encoding="utf-8"
    )

    # A couple of sessions rolling off the trailing edge of the lookback
    # window is normal day-to-day movement, not truncation.
    regressions = calibration._session_count_regressions({"FLKR": {"sample_sessions": 749}}, output)

    assert regressions == {}


def test_standalone_run_refuses_to_publish_when_the_archive_dir_is_missing(tmp_path):
    history_dir = tmp_path / "does-not-exist"
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    artifacts_dir = tmp_path / "artifacts"
    _ticker_config(ticker_config)
    output.write_text('{"status": "existing"}\n', encoding="utf-8")

    result = calibration.rebuild_after_ticker_update(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=output,
        artifacts_dir=artifacts_dir,
        now=datetime(2026, 8, 3, 22, 30, tzinfo=timezone.utc),
        ticker_update_event=tmp_path / "not-required.json",
        force=True,
    )

    assert result["status"] == "blocked_archive_unavailable"
    assert result["archive_unavailable"]["reason"] == "history_dir_missing"
    assert output.read_text(encoding="utf-8") == '{"status": "existing"}\n'
