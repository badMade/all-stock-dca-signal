import datetime as dt
import json

import pytest

from scripts import refresh_active_ticker_histories as refresh

_SAMPLE_TS = int(dt.datetime(2023, 11, 14, tzinfo=dt.timezone.utc).timestamp())


def _ticker_config(path):
    path.write_text(json.dumps({"active": ["ABC"]}), encoding="utf-8")


def test_active_symbols_includes_whole_share_tickers(tmp_path):
    """Codex review ("Include whole-share symbols in history-update
    events"): a whole-share ticker (e.g. BYDDY) is evaluated by the same
    graded ladder and calibrated by generate_per_ticker_sizing.py's
    build_sizing_payload() alongside the active universe -- this refresh
    utility must fetch its history too, or it can never create BYDDY's
    missing history file in the first place.
    """
    config_path = tmp_path / "tickers.json"
    config_path.write_text(
        json.dumps({"active": ["ABC", "DEF"], "whole_share": ["BYDDY"]}), encoding="utf-8"
    )

    assert refresh.active_symbols(config_path) == ["ABC", "DEF", "BYDDY"]


def test_active_symbols_dedups_a_whole_share_ticker_already_in_active(tmp_path):
    config_path = tmp_path / "tickers.json"
    config_path.write_text(
        json.dumps({"active": ["ABC", "BYDDY"], "whole_share": ["BYDDY"]}), encoding="utf-8"
    )

    assert refresh.active_symbols(config_path) == ["ABC", "BYDDY"]


def test_active_symbols_without_whole_share_key_is_just_active(tmp_path):
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)

    assert refresh.active_symbols(config_path) == ["ABC"]


def test_default_history_dir_refuses_to_run_when_the_archive_is_missing(tmp_path, monkeypatch):
    # Codex review ("Do not create a replacement ticker_data tree"): ticker_data/
    # is a gitignored symlink/mount to the managed Google Drive archive.
    # Running this utility without --history-dir while that mount is absent
    # must refuse outright rather than silently writing into (and thereby
    # creating) a local ticker_data stand-in that a later updater or
    # calibration run would mistake for the real archive.
    missing_root = tmp_path / "ticker_data_not_mounted"
    monkeypatch.setattr(refresh, "DEFAULT_HISTORY_DIR", missing_root)
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)
    monkeypatch.setattr(
        "sys.argv",
        ["refresh_active_ticker_histories.py", "--config", str(config_path), "--dry-run"],
    )

    with pytest.raises(SystemExit) as excinfo:
        refresh.main()

    assert excinfo.value.code == 2
    assert not missing_root.exists()


def test_an_explicit_history_dir_is_exempt_from_the_default_archive_guard(tmp_path, monkeypatch):
    # An operator who passes --history-dir explicitly is opting into that
    # location on purpose; the guard only protects the unattended default.
    explicit_dir = tmp_path / "explicit_archive"
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "refresh_active_ticker_histories.py",
            "--config",
            str(config_path),
            "--history-dir",
            str(explicit_dir),
            "--dry-run",
        ],
    )

    exit_code = refresh.main()

    assert exit_code == 0


def test_active_symbols_rejects_invalid_active(tmp_path):
    config_path = tmp_path / "tickers.json"
    config_path.write_text(json.dumps({"active": "not-a-list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain an 'active' list"):
        refresh.active_symbols(config_path)


def test_active_symbols_rejects_invalid_whole_share(tmp_path):
    config_path = tmp_path / "tickers.json"
    config_path.write_text(json.dumps({"active": ["ABC"], "whole_share": [123]}), encoding="utf-8")
    with pytest.raises(ValueError, match="'whole_share' list, if present, must be a list"):
        refresh.active_symbols(config_path)


def test_fetch_chart_success(monkeypatch):
    import subprocess

    chart_payload = {
        "chart": {
            "result": [{"symbol": "ABC", "timestamp": [_SAMPLE_TS]}],
            "error": None,
        }
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=json.dumps(chart_payload), stderr=""
        ),
    )
    result = refresh.fetch_chart("ABC")
    assert result["symbol"] == "ABC"


def test_fetch_chart_empty_result_raises(monkeypatch):
    import subprocess

    chart_payload = {"chart": {"result": [], "error": {"description": "Ticker not found"}}}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=json.dumps(chart_payload), stderr=""
        ),
    )
    with pytest.raises(ValueError, match="Ticker not found"):
        refresh.fetch_chart("INVALID")


def test_chart_rows_success():
    chart = {
        "timestamp": [_SAMPLE_TS],
        "indicators": {
            "quote": [
                {
                    "open": [100.0],
                    "high": [105.0],
                    "low": [98.0],
                    "close": [102.0],
                    "volume": [1000],
                }
            ],
            "adjclose": [{"adjclose": [101.5]}],
        },
        "events": {
            "dividends": {str(_SAMPLE_TS): {"amount": 0.5}},
            "splits": {str(_SAMPLE_TS): {"numerator": 2.0, "denominator": 1.0}},
        },
    }
    rows = refresh.chart_rows(chart)
    assert len(rows) == 1
    assert rows[0]["Open"] == "100.0"
    assert rows[0]["Dividends"] == "0.5"
    assert rows[0]["Stock Splits"] == "2.0"


def test_chart_rows_empty_or_missing_raises():
    with pytest.raises(ValueError, match="no complete daily bars"):
        refresh.chart_rows({"timestamp": [_SAMPLE_TS], "indicators": {}})


def test_write_history(tmp_path):
    out = tmp_path / "hist" / "ABC.csv"
    rows = [
        {
            "Date": "2026-08-01",
            "Open": "10.0",
            "High": "11.0",
            "Low": "9.0",
            "Close": "10.5",
            "Adj Close": "10.5",
            "Volume": "500",
            "Dividends": "0.0",
            "Stock Splits": "0.0",
        }
    ]
    refresh.write_history(out, rows)
    assert out.is_file()
    assert "Date,Open,High" in out.read_text(encoding="utf-8")


def test_main_live_refresh_and_skips(monkeypatch, tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)

    sample_chart = {
        "timestamp": [_SAMPLE_TS],
        "indicators": {
            "quote": [{"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0], "volume": [100]}],
            "adjclose": [{"adjclose": [10.0]}],
        },
    }
    monkeypatch.setattr(refresh, "fetch_chart", lambda sym: sample_chart)

    # 1. First run writes
    monkeypatch.setattr(
        "sys.argv",
        [
            "refresh_active_ticker_histories.py",
            "--config",
            str(config_path),
            "--history-dir",
            str(history_dir),
            "--pause-seconds",
            "0",
        ],
    )
    assert refresh.main() == 0
    assert (history_dir / "ABC.csv").is_file()

    # 2. Second run skips existing
    assert refresh.main() == 0

    # 3. Third run with --force overwrites
    monkeypatch.setattr(
        "sys.argv",
        [
            "refresh_active_ticker_histories.py",
            "--config",
            str(config_path),
            "--history-dir",
            str(history_dir),
            "--force",
            "--pause-seconds",
            "0",
        ],
    )
    assert refresh.main() == 0


def test_main_unknown_symbol_error(monkeypatch, tmp_path):
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "refresh_active_ticker_histories.py",
            "--config",
            str(config_path),
            "--history-dir",
            str(tmp_path),
            "--symbol",
            "NONEXISTENT",
        ],
    )
    with pytest.raises(SystemExit):
        refresh.main()


def test_main_fetch_failure_returns_1(monkeypatch, tmp_path):
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)

    def raise_err(sym):
        raise ValueError("simulated network error")

    monkeypatch.setattr(refresh, "fetch_chart", raise_err)
    monkeypatch.setattr(
        "sys.argv",
        [
            "refresh_active_ticker_histories.py",
            "--config",
            str(config_path),
            "--history-dir",
            str(tmp_path),
            "--pause-seconds",
            "0",
        ],
    )
    assert refresh.main() == 1
