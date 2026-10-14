"""Tests for the per-ticker sizing CLI's archive-availability guard."""

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parents[2]))
from scripts import generate_per_ticker_sizing as sizing  # noqa: E402


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
        json.dumps({"lookback_days": 90, "tickers": {"ABC": {"sample_sessions": 300}}}),
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
    assert json.loads(output.read_bytes())["tickers"]["ABC"]["sample_sessions"] == 300


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
""")
    # 2020-01-02: drop from prior adj close (100) to low (80). adj=1. 80/100 = 20% drop.
    # 2020-01-03: drop from prior adj close (90) to low (10). adj=1. 10/90 = 88% drop. (Ignored as >50%)
    # 2020-01-04: zero values handled.
    drops = sizing.adjusted_low_drops(history_path, "2020-01-01")
    assert len(drops) == 1
    assert abs(drops[0] - 20.0) < 0.01

def test_tiers_from_drops():
    # 96 distinct drops -> every percentile lands on its own threshold, so no ties collapse.
    drops = [float(i) for i in range(1, 97)]
    tiers = sizing.tiers_from_drops(drops)

    assert len(tiers) == len(sizing.PERCENTILE_MULTIPLIERS) == 96
    for t in tiers:
        assert "min_drop_pct" in t
        assert "multiplier" in t

def test_build_sizing_payload(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["A", "B"]}', encoding="utf-8")

    today_str = datetime.date.today().isoformat()

    # A has enough history (MINIMUM_HISTORY_SESSIONS = 40)
    # We need 40 valid drop entries, so 300 rows is ample.
    csv_rows = ["Date,Close,Adj Close,Low"]
    for _ in range(300):
        # We ensure they're past start_date by just using today
        csv_rows.append(f"{today_str},100.0,100.0,90.0")

    (history_dir / "A.csv").write_text("\n".join(csv_rows))

    # B has no history

    payload = sizing.build_sizing_payload(history_dir, ticker_config, lookback_days=90)

    assert "version" in payload
    assert "status" in payload
    assert "tickers" in payload
    assert "A" in payload["tickers"]
    assert "B" in payload["tickers"]

    assert payload["tickers"]["A"]["source"] == "ninety_day_adjusted_daily_low_percentiles"
    assert payload["tickers"]["B"]["source"] == "global_fallback_insufficient_history"

    assert "B" in payload["fallback_tickers"]
    assert "A" not in payload["fallback_tickers"]


def test_percentile_ladder_is_the_owner_specified_96_tier_curve():
    """2026-10-05: one tier per percentile 5th..100th; 8.00x at the 95th, 10.00x at the 100th."""
    ladder = dict(sizing.PERCENTILE_MULTIPLIERS)
    assert len(sizing.PERCENTILE_MULTIPLIERS) == 96
    percentiles = [p for p, _ in sizing.PERCENTILE_MULTIPLIERS]
    assert percentiles == [round(0.05 + i / 100, 2) for i in range(96)]
    multipliers = [m for _, m in sizing.PERCENTILE_MULTIPLIERS]
    assert multipliers == sorted(multipliers)  # never steps down as the drop gets rarer
    # Spot checks against the owner's table.
    assert [ladder[p] for p in (0.05, 0.10, 0.11, 0.25, 0.50, 0.75, 0.90, 0.95, 0.96, 0.99, 1.0)] == [
        1.00, 1.00, 1.03, 1.45, 2.46, 4.17, 6.38, 8.00, 8.17, 9.08, 10.00
    ]


def test_lookback_is_ninety_days_and_hundredth_percentile_is_the_largest_drop(tmp_path):
    assert sizing.LOOKBACK_DAYS == 90
    history_dir = tmp_path / "h"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["A"]}', encoding="utf-8")
    as_of = datetime.date(2026, 10, 5)
    rows = ["Date,Close,Adj Close,Low", "2026-06-01,100,100,100"]  # outside the 90-day window
    for day in range(60):
        rows.append(f"{(as_of - datetime.timedelta(days=60 - day)).isoformat()},100,100,{100 - 1 - (day % 5)}")
    rows.append(f"{(as_of - datetime.timedelta(days=1)).isoformat()},100,100,80")  # the window's worst day: 20%
    (history_dir / "A.csv").write_text("\n".join(rows))
    payload = sizing.build_sizing_payload(history_dir, ticker_config, as_of=as_of)
    assert payload["lookback_days"] == 90
    top = payload["tickers"]["A"]["tiers"][-1]
    assert top == {"min_drop_pct": 20.0, "multiplier": 10.0}


def test_session_count_guard_skips_when_the_lookback_window_changed(tmp_path):
    output = tmp_path / "per_ticker_sizing.json"
    output.write_text(json.dumps({"lookback_days": 1095, "tickers": {"A": {"sample_sessions": 751}}}))
    new = {"A": {"sample_sessions": 63}}
    # The 3-year -> 90-day switch is a deliberate window change, not a truncated archive.
    assert sizing.session_count_regressions(new, output, 90) == {}
    output.write_text(json.dumps({"lookback_days": 90, "tickers": {"A": {"sample_sessions": 63}}}))
    assert sizing.session_count_regressions({"A": {"sample_sessions": 40}}, output, 90)["A"]["new"] == 40


def test_tiers_from_drops_collapses_ties_to_highest_multiplier():
    tiers = sizing.tiers_from_drops([0.0] * 300)  # every percentile clamps to 0.01
    assert tiers == [{"min_drop_pct": 0.01, "multiplier": 10.0}]  # the 100th percentile's 10.00x wins the tie


def test_global_fallback_tiers_match_sizing_ladder_config():
    import json
    from pathlib import Path

    ladder = json.loads((Path(__file__).resolve().parents[2] / "config" / "sizing_ladder.json").read_bytes())
    assert list(sizing.GLOBAL_FALLBACK_TIERS) == [(t["min_drop_pct"], t["multiplier"]) for t in ladder["tiers"]]


def test_render_payload_puts_each_tier_on_one_line_and_round_trips():
    payload = {"tickers": {"A": {"tiers": [{"min_drop_pct": 0.5, "multiplier": 1.0}, {"min_drop_pct": 1.25, "multiplier": 10.0}]}}}
    text = sizing.render_payload(payload)
    assert '{"min_drop_pct": 0.5, "multiplier": 1.0}' in text
    assert json.loads(text) == payload


def test_calibrated_tickers_are_marked_for_linear_interpolation_and_fallback_tickers_are_not(tmp_path):
    history_dir = tmp_path / "h"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["A", "B"]}', encoding="utf-8")
    as_of = datetime.date(2026, 10, 5)
    rows = ["Date,Close,Adj Close,Low"] + [
        f"{(as_of - datetime.timedelta(days=60 - day)).isoformat()},100,100,{99 - (day % 4)}" for day in range(60)
    ]
    (history_dir / "A.csv").write_text("\n".join(rows))
    payload = sizing.build_sizing_payload(history_dir, ticker_config, as_of=as_of)
    assert payload["tickers"]["A"]["interpolation"] == "linear"
    assert "interpolation" not in payload["tickers"]["B"]  # B has no history: global fallback ladder, stepped
