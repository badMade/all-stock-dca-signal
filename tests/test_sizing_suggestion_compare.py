"""Tests for sizing_suggestion_compare: the read-only suggester scorecard."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import sizing_suggestion_compare as cmp
from sizing_suggestion_history import record_suggestions

TICKERS = ["AAA", "BBB", "CCC", "DDD"]
START = date(2026, 1, 5)  # a Monday


def _trading_days(n: int) -> list[date]:
    days, d = [], START
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _write_prices(directory: Path, ticker: str, growth: float, n: int = 40, extra_rows: str = "") -> None:
    """Close = 100 * (1+growth)^i, so the h-day forward return is exactly (1+growth)^h - 1."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits"]
    for i, day in enumerate(_trading_days(n)):
        price = 100 * (1 + growth) ** i
        lines.append(f"{day.isoformat()},{price},{price},{price},{price},{price},1,0,0")
    (directory / f"{ticker}.csv").write_text("\n".join(lines) + "\n" + extra_rows, encoding="utf-8")


@pytest.fixture
def prices_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "prices"
    # AAA and BBB rise, CCC flat, DDD falls: ranking is unambiguous.
    for ticker, growth in zip(TICKERS, (0.02, 0.01, 0.0, -0.01), strict=True):
        _write_prices(directory, ticker, growth)
    return directory


def _record(root: Path, name: str, adjustments: dict, day: date, **kw) -> None:
    record_suggestions(
        name, adjustments, root=root, now=datetime(day.year, day.month, day.day, 20, 0, tzinfo=timezone.utc), **kw
    )


def test_forward_return_uses_last_close_on_or_before_as_of(prices_dir):
    prices = cmp.load_prices("AAA", prices_dir)
    days = _trading_days(40)
    got = cmp.forward_return(prices, days[3], 5)
    assert got == pytest.approx(1.02**5 - 1)
    # A weekend as_of rolls back to Friday's close.
    saturday = days[4] + timedelta(days=1)
    assert cmp.forward_return(prices, saturday, 5) == pytest.approx(1.02**5 - 1)


def test_forward_return_is_none_when_horizon_not_matured_or_before_history(prices_dir):
    prices = cmp.load_prices("AAA", prices_dir)
    days = _trading_days(40)
    assert cmp.forward_return(prices, days[-3], 5) is None
    assert cmp.forward_return(prices, START - timedelta(days=30), 5) is None


def test_load_prices_drops_bad_rows_and_missing_files(prices_dir):
    # The archive's known stray yearless row must not become year 0001.
    _write_prices(prices_dir, "BAD", 0.01, extra_rows="04-27,1,1,1,1,1,1,0,0\n2026-02-30,1,1,1,1,1,1,0,0\n2026-03-02,x,x,x,x,x,1,0,0\n")
    days, closes = cmp.load_prices("BAD", prices_dir)
    assert days[0] == START and len(days) == 40 and min(c for c in closes) > 0
    assert cmp.load_prices("NOPE", prices_dir) is None


def test_a_good_suggester_scores_positive_and_a_bad_one_negative(tmp_path, prices_dir):
    root = tmp_path / "hist"
    for offset in range(3):
        day = _trading_days(40)[offset]
        _record(root, "good", {"AAA": 1.2, "DDD": 0.8}, day)  # up the winner, down the loser
        _record(root, "bad", {"AAA": 0.8, "DDD": 1.2}, day)  # exactly backwards
    report = cmp.compare(["good", "bad"], TICKERS, horizon=5, history_dir=prices_dir, root=root)
    good, bad = report["suggesters"]["good"], report["suggesters"]["bad"]
    assert good["n_runs_scored"] == bad["n_runs_scored"] == 3
    assert good["tilt_weighted_excess"] > 0 > bad["tilt_weighted_excess"]
    assert good["hit_rate"] == 1.0 and bad["hit_rate"] == 0.0
    assert good["mean_ic"] > 0 > bad["mean_ic"]
    assert good["tilt_weighted_excess"] == pytest.approx(-bad["tilt_weighted_excess"])


def test_baseline_is_the_universe_mean_not_zero(tmp_path, prices_dir):
    """Tilting up the second-best ticker still loses to the average if the average is higher -- here a
    tilt on the flat CCC beats zero in raw terms but is judged against the universe mean."""
    root = tmp_path / "hist"
    _record(root, "s", {"BBB": 1.1}, _trading_days(40)[0])
    row = cmp.compare(["s"], TICKERS, horizon=5, history_dir=prices_dir, root=root)["suggesters"]["s"]
    r = {t: (1 + g) ** 5 - 1 for t, g in zip(TICKERS, (0.02, 0.01, 0.0, -0.01), strict=True)}
    baseline = sum(r.values()) / 4
    assert row["tilt_weighted_excess"] == pytest.approx(r["BBB"] - baseline)


def test_pending_runs_are_counted_not_scored(tmp_path, prices_dir):
    root = tmp_path / "hist"
    _record(root, "s", {"AAA": 1.1}, _trading_days(40)[0])
    _record(root, "s", {"AAA": 1.1}, _trading_days(40)[-2])  # horizon not elapsed
    row = cmp.compare(["s"], TICKERS, horizon=5, history_dir=prices_dir, root=root)["suggesters"]["s"]
    assert (row["n_runs_scored"], row["n_runs_pending"]) == (1, 1)


def test_two_runs_same_day_count_once_latest_wins(tmp_path, prices_dir):
    root = tmp_path / "hist"
    day = _trading_days(40)[0]
    _record(root, "s", {"DDD": 1.5}, day)
    _record(root, "s", {"AAA": 1.5}, day)
    row = cmp.compare(["s"], TICKERS, horizon=5, history_dir=prices_dir, root=root)["suggesters"]["s"]
    assert row["n_runs_scored"] == 1 and row["n_scored_suggestions"] == 1 and row["hit_rate"] == 1.0


def test_blend_is_the_equal_weight_mean_and_only_exists_with_two_suggesters(tmp_path, prices_dir):
    root = tmp_path / "hist"
    day = _trading_days(40)[0]
    _record(root, "a", {"AAA": 1.2}, day)
    _record(root, "b", {"AAA": 1.0, "DDD": 0.8}, day)
    assert cmp.blend_tilts({"a": cmp.build_tilts(cmp.latest_runs_by_date(cmp.read_history("a", root=root))),
                            "b": cmp.build_tilts(cmp.latest_runs_by_date(cmp.read_history("b", root=root)))})[day] == {
        "AAA": pytest.approx(0.1), "DDD": pytest.approx(-0.1)
    }
    both = cmp.compare(["a", "b"], TICKERS, horizon=5, history_dir=prices_dir, root=root)
    assert cmp.BLEND_NAME in both["suggesters"]
    only_one = cmp.compare(["a", "missing_stream"], TICKERS, horizon=5, history_dir=prices_dir, root=root)
    assert cmp.BLEND_NAME not in only_one["suggesters"]  # one empty stream is not a blend


def test_empty_history_and_bad_arguments(tmp_path, prices_dir):
    row = cmp.compare(["nothing"], TICKERS, history_dir=prices_dir, root=tmp_path)["suggesters"]["nothing"]
    assert row["n_runs_scored"] == 0 and row["tilt_weighted_excess"] is None and row["hit_rate"] is None
    with pytest.raises(ValueError):
        cmp.compare(["x"], TICKERS, horizon=0, root=tmp_path)
    with pytest.raises(ValueError):
        cmp.compare(["x"], TICKERS, basis="bogus", root=tmp_path)


def test_generated_at_basis_uses_the_source_date_not_the_recording_date(tmp_path, prices_dir):
    root = tmp_path / "hist"
    days = _trading_days(40)
    # Recorded early (matured) but built from source data dated near the end of the price history (pending).
    _record(root, "s", {"AAA": 1.1}, days[0], generated_at=datetime(days[-2].year, days[-2].month, days[-2].day, tzinfo=timezone.utc))
    by_recorded = cmp.compare(["s"], TICKERS, horizon=5, history_dir=prices_dir, root=root)["suggesters"]["s"]
    by_generated = cmp.compare(["s"], TICKERS, horizon=5, basis="generated_at", history_dir=prices_dir, root=root)["suggesters"]["s"]
    assert (by_recorded["n_runs_scored"], by_recorded["n_runs_pending"]) == (1, 0)
    assert (by_generated["n_runs_scored"], by_generated["n_runs_pending"]) == (0, 1)


def test_main_is_read_only_unless_write_and_never_touches_live_config(tmp_path, prices_dir, monkeypatch, capsys):
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(prices_dir))
    monkeypatch.setattr(cmp, "_universe_from_config", lambda: TICKERS)
    record_suggestions("octopus", {"AAA": 1.1}, now=datetime(2026, 1, 6, 20, tzinfo=timezone.utc))
    assert cmp.main(["--horizon", "5"]) == 0
    assert "octopus" in capsys.readouterr().out
    assert not (cmp.history_root() / cmp.COMPARISONS_DIRNAME).exists()
    assert cmp.main(["--horizon", "5", "--write"]) == 0
    written = sorted(p.name for p in (cmp.history_root() / cmp.COMPARISONS_DIRNAME).iterdir())
    assert [Path(n).suffix for n in written] == [".json", ".md"]
    report = json.loads((cmp.history_root() / cmp.COMPARISONS_DIRNAME / written[0]).read_text())
    assert report["schema_version"] == cmp.REPORT_SCHEMA_VERSION
    assert list(tmp_path.rglob("ml_sizing_adjustment.json")) == []


def test_module_never_references_the_live_sizing_file():
    source = Path(cmp.__file__).read_text(encoding="utf-8")
    assert "ml_sizing_adjustment.json" not in source.split('"""', 2)[2]  # docstring may name it; code may not
