"""market_open_quote_shadow: record-only live-quote vs 09:30-bar comparison (docs/market_open_0932_move.md)."""

import json
from datetime import date, datetime, timezone

import market_open_quote_shadow as shadow

PAYLOAD = {
    "AAA": {
        "previous_close_bar": {"close": 100.0},
        "intraday_bars": [{"close": 96.0}],
        "quote": {"price": 95.0, "as_of": "2026-11-09T09:32:00-05:00"},
    },
    "BBB": {"previous_close_bar": {"close": 50.0}, "intraday_bars": [], "quote": {"price": 49.0}},
    "CCC": {"previous_close_bar": {"close": 0}, "quote": {"price": float("nan")}},
}


def test_compare_ticker_signs_and_gap():
    row = shadow.compare_ticker(PAYLOAD["AAA"])
    assert row["quote_drop_vs_prev_close_pct"] == -5.0
    assert row["bar_drop_vs_prev_close_pct"] == -4.0
    assert row["drop_gap_pct_points"] == -1.0
    assert row["quote_minus_bar_pct"] == round((95 - 96) / 96 * 100, 4)


def test_missing_or_invalid_inputs_are_none_not_guessed():
    no_bar = shadow.compare_ticker(PAYLOAD["BBB"])
    assert no_bar["bar_close"] is None and no_bar["drop_gap_pct_points"] is None
    assert no_bar["quote_drop_vs_prev_close_pct"] == -2.0
    bad = shadow.compare_ticker(PAYLOAD["CCC"])
    assert bad["previous_close"] is None and bad["quote_price"] is None
    assert shadow.compare_ticker({"quote": {"price": True}})["quote_price"] is None


def test_record_shadow_appends_jsonl_and_never_rewrites(tmp_path):
    when = datetime(2026, 11, 9, 14, 32, tzinfo=timezone.utc)
    kwargs = dict(trading_day=date(2026, 11, 9), run_id="r1", root=tmp_path, recorded_at=when)
    path = shadow.record_shadow(PAYLOAD, **kwargs)
    first = path.read_text().splitlines()
    assert path.name == "2026.jsonl" and len(first) == 3
    assert [json.loads(line)["ticker"] for line in first] == ["AAA", "BBB", "CCC"]
    shadow.record_shadow(PAYLOAD, **kwargs)
    assert path.read_text().splitlines()[:3] == first and len(path.read_text().splitlines()) == 6


def test_empty_payload_writes_nothing_and_does_not_raise(tmp_path):
    path = shadow.record_shadow({}, trading_day=date(2026, 11, 9), run_id="r", root=tmp_path)
    assert path.read_text() == ""


def test_default_root_honours_cron_tracking_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    assert str(shadow.shadow_root()).startswith(str(tmp_path))


def test_cli_records_and_never_blocks(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "ct"))
    good = tmp_path / "p.json"
    good.write_text(json.dumps({"sources": {"robinhood": PAYLOAD}}))
    assert shadow.main(["--market-data", str(good), "--trading-day", "2026-11-09"]) == 0
    assert "recorded to" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert shadow.main(["--market-data", str(bad), "--trading-day", "2026-11-09"]) == 0
    assert "not recorded" in capsys.readouterr().err
