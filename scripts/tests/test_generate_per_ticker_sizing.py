"""Tests for the per-ticker sizing CLI's archive-availability guard."""

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parents[2]))
from scripts import generate_per_ticker_sizing as sizing


def _ticker_config(path):
    path.write_text(json.dumps({"active": ["ABC"]}), encoding="utf-8")


def test_default_run_refuses_to_publish_when_the_archive_is_missing(tmp_path, monkeypatch, capsys):
    # Codex review ("Refuse default calibration when the archive is
    # missing"): a direct or legacy invocation of this CLI has no upstream
    # caller verifying the history refresh succeeded first. Without the
    # guard, build_sizing_payload treats every ticker as insufficient
    # history and this CLI would overwrite --output with global fallback
    # tiers for the whole active universe.
    missing_history_dir = tmp_path / "ticker_data_not_mounted"
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    _ticker_config(ticker_config)
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_per_ticker_sizing.py",
            "--history-dir",
            str(missing_history_dir),
            "--ticker-config",
            str(ticker_config),
            "--output",
            str(output),
        ],
    )

    exit_code = sizing.main()

    assert exit_code == 1
    assert not output.exists()
    assert "REFUSED" in capsys.readouterr().err


def test_default_run_refuses_to_publish_a_session_count_regression(tmp_path, monkeypatch, capsys):
    # Codex review ("Check session regressions in the standalone publisher"):
    # passing the archive-availability guard only rules out an unmounted or
    # near-empty archive -- it says nothing about a single established
    # ticker's own CSV being truncated by a bad merge while its file still
    # exists (so coverage stays at 100%). Without this check, this CLI would
    # silently overwrite that ticker's published percentile ladder with a
    # collapsed sample.
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    _ticker_config(ticker_config)
    output.write_text(
        json.dumps({"tickers": {"ABC": {"sample_sessions": 300}}}),
        encoding="utf-8",
    )
    rows = ["Date,Open,High,Low,Close,Adj Close,Volume"]
    for day in range(1, 6):
        rows.append(f"2026-08-{day:02d},100,101,99,100,100,1000")
    (history_dir / "ABC.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_per_ticker_sizing.py",
            "--history-dir",
            str(history_dir),
            "--ticker-config",
            str(ticker_config),
            "--output",
            str(output),
        ],
    )

    exit_code = sizing.main()

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr
    assert "ABC" in stderr
    # The stale published payload must survive untouched.
    assert json.loads(output.read_text(encoding="utf-8"))["tickers"]["ABC"]["sample_sessions"] == 300


def test_run_publishes_normally_when_the_archive_has_sufficient_coverage(tmp_path, monkeypatch):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    output = tmp_path / "per_ticker_sizing.json"
    _ticker_config(ticker_config)
    (history_dir / "ABC.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2026-08-03,100,101,99,100,100,1000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_per_ticker_sizing.py",
            "--history-dir",
            str(history_dir),
            "--ticker-config",
            str(ticker_config),
            "--output",
            str(output),
        ],
    )

    exit_code = sizing.main()

    assert exit_code == 0
    assert output.exists()

def test_archive_unavailable_reason_ticker_config_unreadable(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"

    # 1. OSError (file missing)
    reason = sizing.archive_unavailable_reason(history_dir, ticker_config)
    assert reason is not None
    assert "ticker_config_unreadable" in reason["reason"]

    # 2. JSONDecodeError
    ticker_config.write_text("{bad json}", encoding="utf-8")
    reason = sizing.archive_unavailable_reason(history_dir, ticker_config)
    assert reason is not None
    assert "ticker_config_unreadable" in reason["reason"]

    # 3. KeyError
    ticker_config.write_text('{"not_active": []}', encoding="utf-8")
    reason = sizing.archive_unavailable_reason(history_dir, ticker_config)
    assert reason is not None
    assert "ticker_config_unreadable" in reason["reason"]

def test_archive_unavailable_reason_not_active(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": []}', encoding="utf-8")

    # An empty active universe is a valid, deliberate no-op (e.g. the DCA
    # strategy is fully paused) -- scripts/run_ticker_maintenance calibrate
    # depends on this succeeding trivially. See the docstring on this
    # branch in archive_unavailable_reason() for why it isn't refused.
    assert sizing.archive_unavailable_reason(history_dir, ticker_config) is None

def test_archive_unavailable_reason_insufficient_coverage(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["A", "B", "C", "D"]}', encoding="utf-8")

    # Coverage = 0 (all missing)
    reason = sizing.archive_unavailable_reason(history_dir, ticker_config)
    assert reason is not None
    assert reason["reason"] == "insufficient_active_history_coverage"

    # Add one (Coverage = 0.25)
    (history_dir / "A.csv").touch()
    reason = sizing.archive_unavailable_reason(history_dir, ticker_config)
    assert reason is not None
    assert reason["reason"] == "insufficient_active_history_coverage"
    assert reason["coverage"] == 0.25

    # Add another (Coverage = 0.5) => Valid!
    (history_dir / "B.csv").touch()
    assert sizing.archive_unavailable_reason(history_dir, ticker_config) is None

def test_session_count_regressions(tmp_path):
    output = tmp_path / "per_ticker_sizing.json"

    # Missing file
    assert sizing.session_count_regressions({}, output) == {}

    # Bad JSON
    output.write_text("{bad json}")
    assert sizing.session_count_regressions({}, output) == {}

    # No tickers or malformed
    output.write_text('{"tickers": "not_a_dict"}')
    assert sizing.session_count_regressions({}, output) == {}

    # Regression calculations
    output.write_text(json.dumps({
        "tickers": {
            "A": {"sample_sessions": 100},
            "B": {"sample_sessions": 100},
            "C": {"sample_sessions": 100},
            "D": {"sample_sessions": 100},
            "E": "not_a_dict",
            "F": {"sample_sessions": "not_an_int"},
            "G": {"sample_sessions": -10} # Invalid prior
        }
    }))

    new_tickers = {
        "A": {"sample_sessions": 100}, # Same
        "B": {"sample_sessions": 96}, # 4% drop (fine, MAX is 5%)
        "C": {"sample_sessions": 94}, # 6% drop (regression)
        "D": {}, # Missing sample_sessions in new
        "E": {"sample_sessions": 100},
        "F": {"sample_sessions": 100},
        "G": {"sample_sessions": 100},
        "H": {"sample_sessions": 100}, # New ticker
    }

    regressions = sizing.session_count_regressions(new_tickers, output)
    assert "C" in regressions
    assert regressions["C"]["previous"] == 100
    assert regressions["C"]["new"] == 94
    assert len(regressions) == 1

def test_quantile():
    assert sizing.quantile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert sizing.quantile([1.0, 2.0, 3.0], 1.0) == 3.0
    assert sizing.quantile([1.0, 2.0, 3.0], 0.5) == 2.0
    # Interpolation
    assert sizing.quantile([1.0, 2.0, 3.0], 0.25) == 1.5

def test_adjusted_low_drops(tmp_path):
    history_path = tmp_path / "hist.csv"

    # Empty / Bad CSV shouldn't crash
    history_path.write_text("bad,csv\n1,2")
    assert sizing.adjusted_low_drops(history_path, "2020-01-01") == []

    # Missing columns shouldn't crash
    history_path.write_text("Date,Close\n2020-01-01,100")
    assert sizing.adjusted_low_drops(history_path, "2020-01-01") == []

    history_path.write_text("""Date,Close,Adj Close,Low
2020-01-01,100.0,100.0,90.0
2020-01-02,90.0,90.0,80.0
2020-01-03,80.0,80.0,10.0
2020-01-04,0.0,0.0,0.0
2020-01-05,100.0,100.0,95.0
2020-01-06,200.0,100.0,180.0
2020-01-07,100.0
""")
    # 2020-01-02: drop from prior adj close (100) to low (80). adj=1. 80/100 = 20% drop.
    # 2020-01-03: drop from prior adj close (90) to low (10). adj=1. 10/90 = 88% drop. (Ignored as >50%)
    # 2020-01-04: zero values handled (both pairs touching it are skipped).
    # 2020-01-06: close=200, adj close=100 (e.g. a 2:1 split), low=180. Every
    #   other row sets Close == Adj Close, which would let the adjustment
    #   ratio silently drop out of adjusted_low_drops() without this test
    #   noticing. adjusted_low = 180 * 100/200 = 90; drop from the prior adj
    #   close (100) to 90 = 10%. Using the raw low (180) instead would give
    #   a nonsensical negative drop, so this pins the ratio actually being
    #   applied.
    # 2020-01-07: a row truncated below the header (only Date, Close
    #   present) -- the kind of row a corrupted archive merge can leave
    #   behind. Fields past what's present read as None under
    #   csv.DictReader, and float(None) must be caught and the row skipped
    #   rather than raising an uncaught TypeError that would abort
    #   calibration for the whole ticker.
    drops = sizing.adjusted_low_drops(history_path, "2020-01-01")
    assert len(drops) == 2
    assert abs(drops[0] - 20.0) < 0.01
    assert abs(drops[1] - 10.0) < 0.01

def test_tiers_from_drops():
    # PERCENTILE_MULTIPLIERS = ((0.25, 1.0), (0.50, 1.5), (0.75, 2.5), (0.90, 4.0), (0.975, 5.0)).
    # Asserting only shape/keys would still pass if tiers_from_drops()
    # computed the wrong threshold per percentile, paired a threshold with
    # the wrong multiplier, or returned the same value for every tier --
    # since these values directly set order sizing, pin the exact
    # threshold/multiplier sequence for this known, linearly-interpolable
    # input instead.
    drops = [1.0, 2.0, 3.0, 4.0, 5.0]
    tiers = sizing.tiers_from_drops(drops)

    assert tiers == [
        {"min_drop_pct": 2.0, "multiplier": 1.0},
        {"min_drop_pct": 3.0, "multiplier": 1.5},
        {"min_drop_pct": 4.0, "multiplier": 2.5},
        {"min_drop_pct": 4.6, "multiplier": 4.0},
        {"min_drop_pct": 4.9, "multiplier": 5.0},
    ]

def test_build_sizing_payload(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["A", "B"]}', encoding="utf-8")

    today = datetime.date.today()

    # A has enough history (MINIMUM_HISTORY_SESSIONS = 252). Use 300
    # distinct dates rather than one date repeated 300 times: a repeated
    # date still zips into 299 pairs today, so it would keep "passing" even
    # if malformed duplicate-date rows were accepted in production, and
    # would break against a future fix that deduplicates by date.
    csv_rows = ["Date,Close,Adj Close,Low"]
    for offset in range(300):
        # We ensure they're past start_date by counting back from today.
        day = today - datetime.timedelta(days=offset)
        csv_rows.append(f"{day.isoformat()},100.0,100.0,90.0")

    (history_dir / "A.csv").write_text("\n".join(csv_rows))

    # B has no history

    payload = sizing.build_sizing_payload(history_dir, ticker_config, lookback_years=3)

    assert "version" in payload
    assert "status" in payload
    assert "tickers" in payload
    assert "A" in payload["tickers"]
    assert "B" in payload["tickers"]

    assert payload["tickers"]["A"]["source"] == "three_year_adjusted_daily_low_percentiles"
    assert payload["tickers"]["B"]["source"] == "global_fallback_insufficient_history"

    assert "B" in payload["fallback_tickers"]
    assert "A" not in payload["fallback_tickers"]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
