"""Tests for the per-ticker sizing CLI's archive-availability guard."""

import json

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
