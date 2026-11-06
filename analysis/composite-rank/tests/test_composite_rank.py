"""Hermetic tests for analysis/composite-rank/composite_rank.py (synthetic prices, no network, no archive)."""

import datetime as dt
import importlib.util
import json
import math
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "composite_rank", Path(__file__).resolve().parents[1] / "composite_rank.py"
)
cr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cr)

AS_OF = dt.date(2026, 11, 4)


def make_series(daily_growth, days=420, wobble=0.0, end=AS_OF):
    """Daily bars ending on ``end``; ``wobble`` alternates +/- to create volatility."""
    points = []
    value = 100.0
    for i in range(days):
        day = end - dt.timedelta(days=days - 1 - i)
        value *= 1.0 + daily_growth + (wobble if i % 2 else -wobble)
        points.append((day, value))
    return cr.Series(points)


def test_percentiles_rank_one_is_best_and_ties_share_a_rank():
    pct = cr.percentiles({"a": 5.0, "b": 3.0, "c": 3.0, "d": 1.0})
    assert pct["a"] == 100.0
    assert pct["b"] == pct["c"] == pytest.approx(100.0 * (1 - 1 / 3))
    assert pct["d"] == 0.0
    assert cr.percentiles({"only": 1.0}) == {"only": 100.0}


def test_hurdle_bonus_applies_only_at_twenty_percent_annualised():
    below = cr.period_score(50.0, 0.18, "1y")  # 18% annualised: no bonus
    above = cr.period_score(50.0, 0.21, "1y")
    assert above - below > cr.HURDLE_BONUS  # bonus plus the extra return score
    # a +2% month annualises to roughly 27%, so it clears the hurdle; -2% earns no return score
    assert cr.period_score(0.0, 0.02, "1m") > cr.period_score(0.0, -0.02, "1m") + cr.HURDLE_BONUS
    assert cr.return_score(-0.5) == 0.0 and cr.return_score(5.0) == 100.0
    assert cr.annualise(-1.0, 30) == -1.0


def test_truncate_ignores_bars_after_as_of():
    """Regression: the archive can already hold a newer, partial day; it must not count as 'today'."""
    series = make_series(0.001, days=60)
    extra = cr.Series(list(zip(series.dates, series.values, strict=False)) + [(AS_OF + dt.timedelta(days=1), 9999.0)])
    clipped = extra.truncate(AS_OF)
    assert clipped.last_date == AS_OF
    assert clipped.last_value == pytest.approx(series.last_value)


def test_steady_riser_beats_volatile_and_flat_names():
    universe = {
        "STEADY": make_series(0.0010, wobble=0.0005),
        "WILD": make_series(0.0010, wobble=0.04),
        "FLAT": make_series(0.0, wobble=0.0005),
        "FALLING": make_series(-0.001, wobble=0.0005),
    }
    records, unscored = cr.score_universe(universe, AS_OF)
    assert unscored == []
    assert records["STEADY"]["rank"] == 1
    assert records["STEADY"]["vol"] < records["WILD"]["vol"]
    assert records["FALLING"]["rank"] == 4
    assert records["STEADY"]["periods"] == 5  # no 5-year history in the synthetic data


def test_short_history_and_stale_tickers_are_not_scored():
    universe = {
        "OK": make_series(0.001),
        "SHORT": make_series(0.001, days=60),
        "STALE": make_series(0.001, end=AS_OF - dt.timedelta(days=30)),
    }
    records, unscored = cr.score_universe(universe, AS_OF)
    assert set(records) == {"OK"}
    assert set(unscored) == {"SHORT", "STALE"}


def _records(order):
    return {
        t: {"rank": i, "score": 100.0 - i, "vol": 0.2, "max_dd": -0.1, "returns": {"1y": 0.3}, "periods": 6}
        for i, t in enumerate(order, 1)
    }


def test_history_upsert_is_idempotent_and_movement_signs(tmp_path):
    path = tmp_path / "history.csv"
    day1, day2 = dt.date(2026, 11, 3), dt.date(2026, 11, 4)
    cr.upsert_history(path, day1, _records(["A", "B", "C"]))
    cr.upsert_history(path, day2, _records(["B", "A", "C"]))
    rows = cr.upsert_history(path, day2, _records(["B", "A", "C"]))  # re-run the same day
    assert len(rows) == 6
    summary = cr.movement_summary(rows, threshold=98.5)
    assert summary["tickers"]["B"]["chg_1d"] == 1  # climbed from 2 to 1
    assert summary["tickers"]["A"]["chg_1d"] == -1
    assert summary["tickers"]["C"]["avg_abs_move"] == 0
    assert "1-10" in summary["bands"]
    assert "Typical movement by starting rank" in cr.render_movement_md(summary, 98.5)
    one_run = cr.render_movement_md(cr.movement_summary(rows[:3], 98.5), 98.5)
    assert "Only one run" in one_run


def test_band_labels():
    assert cr.band_of(1) == "1-10" and cr.band_of(11) == "11-25" and cr.band_of(250) == "201+"


def test_evaluation_list_reports_new_and_dropped_names():
    records = _records(["A", "B", "C"])
    prev = {"A": (1, 99.0), "Z": (2, 95.0)}
    text = cr.render_evaluation_md(
        dt.date(2026, 11, 4), records, {"A": "Alpha"}, {"A"}, 90.0, 0, "2026-11-03", prev, {}
    )
    assert "New: B, C" in text and "Dropped: Z" in text and "Alpha" in text


def test_ingest_bars_skips_interpolated_bars(tmp_path):
    payload = {"data": {"results": [{"symbol": "XYZ", "bars": [
        {"begins_at": "2026-11-03T00:00:00Z", "close_price": "10.5"},
        {"begins_at": "2026-11-04T00:00:00Z", "close_price": "10.6", "interpolated": True},
    ]}]}}
    path = tmp_path / "bars.json"
    path.write_text(json.dumps(payload))
    assert cr.ingest_bars([path]) == {"XYZ": [["2026-11-03", 10.5]]}


def test_parse_sp500_html_rejects_a_changed_page():
    with pytest.raises(ValueError):
        cr.parse_sp500_html("<html>nothing here</html>")
    rows = "".join(f"<tr><td>T{i}</td><td>Co {i}</td><td>Sector</td></tr>" for i in range(450))
    page = f'<table id="constituents"><tr><th>h</th></tr>{rows}</table>'
    parsed = cr.parse_sp500_html(page)
    assert len(parsed) == 450 and parsed[0] == ("T0", "Co 0", "Sector")


def test_full_run_writes_outputs(tmp_path):
    data = tmp_path / "data"
    (data / "inputs").mkdir(parents=True)
    (data / "inputs" / "holdings.json").write_text(json.dumps({"symbols": ["AAA", "BBB"]}))
    (data / "inputs" / "sp500.json").write_text(json.dumps([["CCC", "Gamma", "Tech"]]))
    ext = {t: [[d.isoformat(), v] for d, v in zip(s.dates, s.values, strict=False)]
           for t, s in {"AAA": make_series(0.001), "BBB": make_series(-0.001), "CCC": make_series(0.0005)}.items()}
    (data / "inputs" / "ext_prices.json").write_text(json.dumps(ext))
    archive = tmp_path / "empty_archive"
    archive.mkdir()
    code = cr.main(["--data-dir", str(data), "--archive-dir", str(archive), "--universe", "all", "run",
                    "--as-of", "2026-11-04", "--threshold", "10"])
    assert code == 0
    assert (data / "runs" / "2026-11-04" / "ranked_all.csv").exists()
    assert (data / "history.csv").exists() and (data / "movement_summary.md").exists()
    assert "Evaluation list for 2026-11-04" in (data / "latest_evaluation_list.md").read_text()
    assert math.isfinite(float((data / "latest_ranked_all.csv").read_text().splitlines()[1].split(",")[5]))


def test_universe_modes_and_backfill(tmp_path):
    data = tmp_path / "data"
    (data / "inputs").mkdir(parents=True)
    (data / "inputs" / "holdings.json").write_text(json.dumps({"symbols": ["AAA", "ETF1"]}))
    (data / "inputs" / "sp500.json").write_text(json.dumps([["AAA", "Alpha", "Tech"], ["CCC", "Gamma", "Tech"]]))
    tickers, held, _, _ = cr.build_universe(data, "sp500")
    assert tickers == ["AAA", "CCC"] and held == {"AAA", "ETF1"}  # ETF1 is held but outside the S&P 500
    assert cr.build_universe(data, "all")[0] == ["AAA", "CCC", "ETF1"]
    with pytest.raises(ValueError):
        cr.build_universe(data, "everything")

    bars = {t: [[d.isoformat(), v] for d, v in zip(s.dates, s.values, strict=False)]
            for t, s in {"AAA": make_series(0.001), "CCC": make_series(0.0005)}.items()}
    (data / "inputs" / "ext_prices.json").write_text(json.dumps(bars))
    archive = tmp_path / "empty_archive"
    archive.mkdir()
    base = ["--data-dir", str(data), "--archive-dir", str(archive)]
    assert cr.main(base + ["backfill", "--start", "2026-10-26", "--end", "2026-11-04"]) == 0
    dates = {r["date"] for r in cr.read_history(data / "history.csv")}
    assert len(dates) == 10 and "2026-11-04" in dates  # every calendar day here has a synthetic bar
    assert {r["ticker"] for r in cr.read_history(data / "history.csv")} == {"AAA", "CCC"}


def test_history_log_has_no_held_column_and_migrates_old_rows(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text("date,ticker,held,score,rank,universe_size,vol,max_dd\n2026-11-03,A,1,99.0,1,2,0.2,-0.1\n")
    rows = cr.upsert_history(path, dt.date(2026, 11, 4), _records(["A", "B"]))
    header = path.read_text().splitlines()[0]
    assert "held" not in header and header == ",".join(cr.HISTORY_FIELDS)
    assert len(rows) == 3  # the old row survived the migration


def test_history_command_prints_compact_lines(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    cr.upsert_history(data / "history.csv", dt.date(2026, 11, 3), _records(["A", "B", "C"]))
    cr.upsert_history(data / "history.csv", dt.date(2026, 11, 4), _records(["B", "A", "C"]))
    base = ["--data-dir", str(data)]
    assert cr.main(base + ["history", "--ticker", "a", "zzz"]) == 0
    out = capsys.readouterr().out
    assert "A: 2 runs, rank now 2 (best 1, worst 2" in out and "2026-11-04 #2" in out
    assert "ZZZ: no history" in out
    assert cr.main(base + ["history", "--date", "2026-11-04", "--top", "2"]) == 0
    top = capsys.readouterr().out.splitlines()
    assert top[0] == "2026-11-04 top 2 of 3" and top[1].startswith("#  1 B") and len(top) == 3
    assert cr.main(base + ["history", "--date", "2026-01-01"]) == 1


# ---- tunable params, daily trade list, review-first proposal, effectiveness ----------------------
def _scored(scores):
    return {t: {"score": s, "rank": i} for i, (t, s) in enumerate(scores.items(), 1)}


def test_params_load_validate_and_change_scores(tmp_path):
    base = cr.load_params()
    assert base["version"] and base["evaluate_threshold"] == 80.0 and base["held_threshold"] == 50.0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"blend": {"base": 0.5, "lowvol": 0.2, "lowdd": 0.1}}))
    with pytest.raises(ValueError):
        cr.load_params(bad)
    bad.write_text(json.dumps({"held_threshold": 90}))
    with pytest.raises(ValueError):
        cr.load_params(bad)
    universe = {"UP": make_series(0.0015, wobble=0.0), "FLAT": make_series(0.0, wobble=0.0),
                "DOWN": make_series(-0.0005, wobble=0.0)}
    default, _ = cr.score_universe(universe, AS_OF)
    custom = tmp_path / "p.json"
    flat_weights = {"1y": 1, "1m": 1, "1w": 1, "3m": 1, "6m": 1, "5y": 1}
    custom.write_text(json.dumps({"version": "x", "period_weights": flat_weights,
                                  "blend": {"base": 1.0, "lowvol": 0.0, "lowdd": 0.0}}))
    tuned, _ = cr.score_universe(universe, AS_OF, cr.load_params(custom))
    assert default["UP"]["score"] != tuned["UP"]["score"]
    assert default["UP"]["rank"] == tuned["UP"]["rank"] == 1


def test_trade_list_is_eval_names_plus_held_above_floor():
    params = cr.load_params()
    recs = _scored({"A": 91.0, "B": 85.0, "C": 70.0, "D": 55.0, "E": 49.0, "F": 80.0})
    trade = dict(cr.build_trade_list(recs, {"B", "D", "E"}, params))
    assert trade == {"A": "above_eval", "B": "above_eval_held", "D": "held_above_floor"}
    assert "C" not in trade and "E" not in trade and "F" not in trade  # 80.0 is not above 80


def test_proposal_is_a_diff_and_never_touches_config(tmp_path):
    params = cr.load_params()
    config = {"active": ["A", "X"], "excluded": ["Q"], "whole_share": ["W"]}
    trade = [("A", "above_eval"), ("B", "above_eval"), ("Q", "above_eval")]
    proposal = cr.build_proposal(AS_OF, trade, config, params)
    assert proposal["add"] == ["B"] and proposal["stop_buying"] == ["X"] and proposal["keep"] == ["A"]
    assert proposal["skipped_excluded_or_whole_share"] == ["Q"]
    assert "REVIEW FIRST" in cr.render_proposal_md(proposal)


def test_run_writes_trade_list_history_and_proposal_and_stamps_version(tmp_path):
    data = tmp_path / "data"
    (data / "inputs").mkdir(parents=True)
    (data / "inputs" / "holdings.json").write_text(json.dumps({"symbols": ["BBB"]}))
    constituents = [["AAA", "A", "T"], ["BBB", "B", "T"], ["CCC", "C", "T"]]
    (data / "inputs" / "sp500.json").write_text(json.dumps(constituents))
    ext = {t: [[d.isoformat(), v] for d, v in zip(s.dates, s.values, strict=False)]
           for t, s in {"AAA": make_series(0.001), "BBB": make_series(-0.0002), "CCC": make_series(0.0004)}.items()}
    (data / "inputs" / "ext_prices.json").write_text(json.dumps(ext))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "tickers.json").write_text(json.dumps({"active": ["CCC"], "excluded": [], "whole_share": []}))
    archive = tmp_path / "empty"
    archive.mkdir()
    argv = ["--data-dir", str(data), "--archive-dir", str(archive), "--config-dir", str(cfg),
            "run", "--as-of", "2026-11-04"]
    assert cr.main(argv) == 0 and cr.main(argv) == 0  # idempotent per date
    rows = cr.read_history(data / "trade_list_history.csv")
    version = cr.load_params()["version"]
    assert len(rows) == len({r["ticker"] for r in rows}) and {r["params_version"] for r in rows} == {version}
    summary = json.loads((data / "runs" / "2026-11-04" / "summary.json").read_text())
    assert summary["params_version"] == cr.load_params()["version"] and "trade_list" in summary
    proposal = json.loads((data / "runs" / "2026-11-04" / "tickers_proposal.json").read_text())
    assert proposal["active_now"] == 1
    assert json.loads((cfg / "tickers.json").read_text())["active"] == ["CCC"]  # read-only


def test_effectiveness_compares_list_with_universe(tmp_path, capsys):
    end = AS_OF + dt.timedelta(days=40)
    up, down = make_series(0.002, days=500, end=end), make_series(-0.002, days=500, end=end)
    rows = [{"date": AS_OF.isoformat(), "ticker": "UP", "params_version": "v"}]
    result = cr.effectiveness(rows, {"UP": up, "DOWN": down}, ["UP", "DOWN"], 20)
    assert result and result[0]["edge"] > 0 and result[0]["listed"] == 1
    assert cr.effectiveness(rows, {"UP": up, "DOWN": down}, ["UP", "DOWN"], 400) == []
