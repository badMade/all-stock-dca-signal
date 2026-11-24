"""Shadow writer rules (ADR D8), the walk-forward backtest, the D7 report and the CLI."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from kalman_signals import backtest_cli
from kalman_signals.backtest import BUCKET_NAMES, bucket_of, report, walk_forward
from kalman_signals.residual_dip import ResidualDipInputs, residual_dip_shadow
from kalman_signals.shadow import (
    SHADOW_FILENAME,
    ShadowRecord,
    ShadowResult,
    ShadowWriter,
    write_shadow_result,
)
from tests.kalman._synth import MKT, SMALL, generate

D = date(2026, 1, 5)


def _record(**payload):
    return ShadowRecord(as_of=D, inputs_known_at=D, payload={"ticker": "S1", **payload})


def test_writer_appends_once_and_marks_no_live_effect(tmp_path):
    writer = ShadowWriter(tmp_path)
    first, again = writer.append(_record(z=-2.5)), writer.append(_record(z=-2.5))
    assert first.written and first.status == "written"
    assert not again.written and again.status == "duplicate"
    lines = (tmp_path / SHADOW_FILENAME).read_text().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line["live_effect"] == "none" and line["signal"] == "kalman_residual_dip"
    assert line["content_hash"] == first.content_hash


def test_writer_refuses_a_config_directory(tmp_path):
    with pytest.raises(ValueError, match="config"):
        ShadowWriter(tmp_path / "config" / "shadow")


def test_writer_refuses_lookahead_and_non_finite(tmp_path):
    writer = ShadowWriter(tmp_path)
    with pytest.raises(ValueError, match="after as_of"):
        writer.append(ShadowRecord(as_of=D, inputs_known_at=date(2026, 1, 6), payload={}))
    with pytest.raises(ValueError, match="finite"):
        writer.append(_record(z=math.nan))
    assert not (tmp_path / SHADOW_FILENAME).exists()


def test_writer_refuses_an_unterminated_tail(tmp_path):
    (tmp_path / SHADOW_FILENAME).write_text('{"content_hash": "x"}')
    with pytest.raises(ValueError, match="unterminated"):
        ShadowWriter(tmp_path).append(_record())


def test_failed_result_writes_nothing(tmp_path):
    failed = ShadowResult(as_of=D, ok=False, reason="stale_market", records=(_record(),))
    assert write_shadow_result(failed, ShadowWriter(tmp_path)) == []
    assert not (tmp_path / SHADOW_FILENAME).exists()


def test_end_to_end_shadow_write(tmp_path):
    sessions, rows = generate(320, {"S1": 1.2, "S2": 0.8})
    result = residual_dip_shadow(ResidualDipInputs(rows), sessions[-1], SMALL)
    written = write_shadow_result(result, ShadowWriter(tmp_path))
    assert [w.status for w in written] == ["written", "written"]


# --- backtest --------------------------------------------------------------------------

SESSIONS, ROWS = generate(420, {"S1": 1.3, "S2": 0.7}, seed=21)


def test_bucket_edges():
    assert [bucket_of(z) for z in (-3.0, -2.0, -1.5, -0.1, 0.5, 2.0)] == [
        "z<=-2", "z<=-2", "-2<z<=-1", "-1<z<=0", "0<z<=1", "z>1"]


def test_first_session_of_each_block_matches_the_single_day_shadow():
    start = SESSIONS[300]
    events = walk_forward(ROWS, start, SESSIONS[-1], SMALL)
    first_day = min(e.session for e in events)
    shadow = residual_dip_shadow(ResidualDipInputs(ROWS), first_day, SMALL)
    expected = {r.payload["ticker"]: r.payload["market"]["z_total"] for r in shadow.records}
    got = {e.ticker: e.z_total for e in events if e.session == first_day}
    assert got == pytest.approx(expected, rel=1e-12)


def test_walk_forward_scores_every_session_and_has_forward_outcomes():
    start = SESSIONS[300]
    events = walk_forward(ROWS, start, SESSIONS[-1], SMALL, horizon=5)
    sessions = sorted({e.session for e in events})
    assert sessions[0] >= start and sessions[-1] == SESSIONS[-1]
    assert len(events) == 2 * len(sessions)
    tail = [e for e in events if e.session in sessions[-5:]]
    assert all(e.forward_adjusted is None for e in tail)
    assert any(e.forward_adjusted is not None for e in events)


def test_walk_forward_excludes_non_equity():
    events = walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL, non_equity=frozenset({"S2"}))
    assert {e.ticker for e in events} == {"S1"}


def test_walk_forward_refuses_too_short_a_market_history():
    with pytest.raises(ValueError):
        walk_forward(ROWS[:200], SESSIONS[10], SESSIONS[40], SMALL)


def test_report_shape_and_calibration_on_synthetic_data():
    events = walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL)
    result = report(events, SMALL.q_grid)
    assert result["events"] == len(events)
    assert result["calibration"]["variance"] == pytest.approx(1.0, abs=0.3)
    assert set(result["forward_adjusted_by_intraday_z"]) == set(BUCKET_NAMES)
    assert result["trials"]["count"] == len(SMALL.q_grid)
    # ~5 months and ~240 events: the size checks must fail, so the gate cannot pass on toy data.
    assert result["gate"]["at_least_1300_events_per_bucket"] is False
    assert result["gate_passed"] is False
    json.dumps(result)  # serialisable


def test_report_on_no_events():
    result = report([], SMALL.q_grid)
    assert result["events"] == 0 and result["gate_passed"] is False


# --- CLI -------------------------------------------------------------------------------
#
# The CLI runs the default (504-session) config, so it needs a longer synthetic history.
LONG_SESSIONS, LONG_ROWS = generate(600, {"S1": 1.3, "S2": 0.7}, seed=22)
LONG_START = LONG_SESSIONS[560]


def _write_archive(directory, rows):
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    for ticker, items in by_ticker.items():
        with (directory / f"{ticker}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"])
            for r in items:
                # Adj Close is half the raw close (a 2:1 split adjustment) so the loader's
                # scaling of the open is exercised.
                w.writerow([r["date"].isoformat(), r["open"] * 2, 0, 0, r["close"] * 2, r["close"], 0, 0, 0])
            w.writerow(["04-27", "", "", "", "", "", "", "", ""])  # a yearless archive row (seen in CBU.csv)


def test_cli_end_to_end(tmp_path):
    _write_archive(tmp_path, LONG_ROWS)
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    code = backtest_cli.main([
        "--history-dir", str(tmp_path), "--market", MKT, "--tickers", "S1,S2,NOPE",
        "--start", LONG_START.isoformat(), "--end", LONG_SESSIONS[-1].isoformat(),
        "--out-json", str(out_json), "--out-md", str(out_md),
    ])
    assert code == 0
    result = json.loads(out_json.read_text())
    assert result["market_ticker"] == MKT and result["events"] > 0
    assert result["load"]["NOPE"] == "missing_file"
    assert result["load"]["S1"] == "skipped_rows=1"
    assert "Gate passed" in out_md.read_text()


def test_cli_scores_match_the_pure_path(tmp_path):
    # The loader's split scaling must leave returns unchanged.
    _write_archive(tmp_path, LONG_ROWS)
    out = tmp_path / "r.json"
    backtest_cli.main(["--history-dir", str(tmp_path), "--market", MKT, "--tickers", "S1,S2",
                       "--start", LONG_START.isoformat(), "--end", LONG_SESSIONS[-1].isoformat(),
                       "--out-json", str(out)])
    config = replace(SMALL, window_sessions=504, burn_in=60, min_train=120)
    pure = report(walk_forward(LONG_ROWS, LONG_START, LONG_SESSIONS[-1], config), config.q_grid)
    assert json.loads(out.read_text())["calibration"] == pytest.approx(pure["calibration"], rel=1e-9)


def test_cli_needs_the_market_file(tmp_path):
    code = backtest_cli.main(["--history-dir", str(tmp_path), "--market", "SPY", "--tickers", "S1",
                              "--start", "2024-01-01", "--end", "2024-02-01", "--out-json", str(tmp_path / "x")])
    assert code == 2


# --- earnings windows, labels, --market-file ----------------------------------------------


def test_earnings_window_flags_pad_sessions_around_each_announcement():
    start = SESSIONS[300]
    announce = SESSIONS[320]
    events = walk_forward(ROWS, start, SESSIONS[-1], SMALL, earnings_dates={"S1": [announce]}, earnings_pad=1)
    flagged = sorted(e.session for e in events if e.earnings_window)
    assert flagged == [SESSIONS[319], SESSIONS[320], SESSIONS[321]]
    assert all(e.ticker == "S1" for e in events if e.earnings_window)


def test_earnings_on_a_non_session_date_anchors_on_the_next_session():
    # A Saturday announcement: its first session is the following Monday.
    monday = next(s for s in SESSIONS[310:] if s.weekday() == 0)
    saturday = monday - timedelta(days=2)
    events = walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL, earnings_dates={"S2": [saturday]},
                          earnings_pad=0)
    assert [e.session for e in events if e.earnings_window] == [monday]


def test_flagging_earnings_never_changes_any_score():
    plain = walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL)
    marked = walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL, earnings_dates={"S1": SESSIONS[300::7]})
    assert [replace(e, earnings_window=False) for e in marked] == plain


def test_negative_earnings_pad_is_refused():
    with pytest.raises(ValueError, match="earnings_pad"):
        walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL, earnings_dates={"S1": [SESSIONS[320]]},
                     earnings_pad=-1)


def test_report_has_forward_returns_by_label():
    result = report(walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL), SMALL.q_grid)
    by_label = result["forward_adjusted_by_label"]
    assert sum(b["events"] for b in by_label.values()) == sum(
        1 for e in walk_forward(ROWS, SESSIONS[300], SESSIONS[-1], SMALL) if e.forward_adjusted is not None)


def test_cli_earnings_file_excludes_windows_and_reports_both(tmp_path):
    _write_archive(tmp_path, LONG_ROWS)
    announce = LONG_SESSIONS[570]
    (tmp_path / "earn.json").write_text(json.dumps({"dates": {"S1": [announce.isoformat()]}}))
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    base = ["--history-dir", str(tmp_path), "--market", MKT, "--tickers", "S1,S2",
            "--start", LONG_START.isoformat(), "--end", LONG_SESSIONS[-1].isoformat()]
    assert backtest_cli.main([*base, "--out-json", str(tmp_path / "plain.json")]) == 0
    assert backtest_cli.main([*base, "--earnings", str(tmp_path / "earn.json"),
                              "--out-json", str(out_json), "--out-md", str(out_md)]) == 0
    plain, result = json.loads((tmp_path / "plain.json").read_text()), json.loads(out_json.read_text())
    assert result["earnings"]["excluded_events"] == 3
    assert result["events"] == plain["events"] - 3
    assert result["earnings"]["calibration_including_earnings"] == pytest.approx(plain["calibration"], rel=1e-12)
    assert "Earnings windows" in out_md.read_text()
    plain_md = tmp_path / "plain.md"
    backtest_cli.main([*base, "--out-json", str(tmp_path / "x.json"), "--out-md", str(plain_md)])
    assert "earnings days are included" in plain_md.read_text()


def test_cli_market_file_outside_the_history_dir(tmp_path):
    archive, elsewhere = tmp_path / "archive", tmp_path / "market"
    archive.mkdir(), elsewhere.mkdir()
    _write_archive(archive, [r for r in LONG_ROWS if r["ticker"] != MKT])
    _write_archive(elsewhere, [r for r in LONG_ROWS if r["ticker"] == MKT])
    out = tmp_path / "r.json"
    args = ["--history-dir", str(archive), "--market", MKT, "--tickers", "S1,S2",
            "--start", LONG_START.isoformat(), "--end", LONG_SESSIONS[-1].isoformat(), "--out-json", str(out)]
    assert backtest_cli.main(args) == 2  # not in the archive
    assert backtest_cli.main([*args, "--market-file", str(elsewhere / f"{MKT}.csv")]) == 0
    result = json.loads(out.read_text())
    assert result["market_file"] == f"{MKT}.csv" and result["events"] > 0


def test_gate_checks_each_volatility_regime_not_just_the_pool():
    # ADR D7.1: calibration "by volatility regime". A pooled variance of 1.0 built from a
    # 0.7 regime and a 1.3 regime must not pass.
    from kalman_signals.backtest import Event

    def ev(i, z, high):
        return Event("S1", SESSIONS[i % 400], None, z, None, 1e-4, None, high)
    zs_low = [x * math.sqrt(0.7) for x in (-1.0, 1.0) * 500]
    zs_high = [x * math.sqrt(1.3) for x in (-1.0, 1.0) * 500]
    events = [ev(i, z, False) for i, z in enumerate(zs_low)] + [ev(i, z, True) for i, z in enumerate(zs_high)]
    gate = report(events, SMALL.q_grid)["gate"]
    assert gate["variance_0_9_to_1_1"] is True
    assert gate["variance_0_9_to_1_1_each_vol_regime"] is False
