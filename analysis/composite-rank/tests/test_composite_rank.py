"""Hermetic tests for analysis/composite-rank/composite_rank.py (synthetic prices, no network, no archive)."""

import datetime as dt
import importlib.util
import json
import math
import os
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


# ---- shadow logging of forward-looking models (zero weight) ---------------------------------------
OCTOPUS_HEADER = "ticker,model,origin_date,ref_close,pred_close,close_p10,close_p90,expected_target_date"


def _octopus_csv(path, rows):
    path.write_text(OCTOPUS_HEADER + "\n" + "\n".join(",".join(str(v) for v in r) for r in rows) + "\n")


def test_zero_forward_weight_never_changes_the_score_and_positive_weight_does():
    assert cr.apply_forward(70.0, {"octopus": 100.0}, {"octopus": 0.0, "ml": 0.0}) == 70.0
    assert cr.apply_forward(70.0, None, {"octopus": 0.5, "ml": 0.0}) == 70.0
    assert cr.apply_forward(70.0, {"octopus": 100.0}, {"octopus": 0.25, "ml": 0.0}) == pytest.approx(77.5)
    with pytest.raises(ValueError):
        bad = {"forward_weights": {"octopus": 0.8, "ml": 0.5}}
        path = Path(__file__).parent / "_bad_params.json"
        path.write_text(json.dumps(bad))
        try:
            cr.load_params(path)
        finally:
            path.unlink()
    assert cr.load_params()["forward_weights"] == {"octopus": 0.0, "ml": 0.0}


def test_octopus_export_is_logged_idempotently_and_scored_in_a_shadow_scorecard(tmp_path):
    day, target = "2026-11-04", "2026-11-05"
    names = ["UP", "A", "B", "C", "D", "E", "DOWN"]
    rows = [(t, "octopus", day, 100, 100 * (1 + (3 - i) / 100), 99, 101, target) for i, t in enumerate(names)]
    csv_path = tmp_path / "daily" / f"{day}_next_day_forecasts.csv"
    csv_path.parent.mkdir()
    _octopus_csv(csv_path, rows)
    data = tmp_path / "data"
    argv = ["--data-dir", str(data), "shadow-octopus", str(tmp_path)]
    assert cr.main(argv) == 0 and cr.main(argv) == 0
    logged = cr.read_history(data / "shadow_octopus_history.csv")
    assert len(logged) == len(names) and {r["origin_date"] for r in logged} == {day}
    assert float(next(r for r in logged if r["ticker"] == "UP")["fwd_pct"]) == 100.0
    # realised: the forecast ranking is right (UP rises most), so the rank correlation is positive
    series = {}
    for i, t in enumerate(names):
        series[t] = cr.Series([(dt.date(2026, 11, 4), 100.0), (dt.date(2026, 11, 5), 100.0 * (1 + (3 - i) / 100))])
    card = cr.shadow_scorecard(logged, [], series)
    assert card and card[0]["octopus_ic"] == pytest.approx(1.0) and card[0]["octopus_spread"] > 0
    assert cr.main(["--data-dir", str(tmp_path / "none"), "shadow-octopus", str(tmp_path / "missing")]) == 1


def test_run_consumes_octopus_sources_but_leaves_scores_unchanged_at_zero_weight(tmp_path):
    def run(with_octopus):
        data = tmp_path / ("with" if with_octopus else "without")
        (data / "inputs").mkdir(parents=True)
        (data / "inputs" / "holdings.json").write_text(json.dumps({"symbols": []}))
        members = [["AAA", "A", "T"], ["BBB", "B", "T"], ["CCC", "C", "T"]]
        (data / "inputs" / "sp500.json").write_text(json.dumps(members))
        ext = {t: [[d.isoformat(), v] for d, v in zip(s.dates, s.values, strict=False)]
               for t, s in {"AAA": make_series(0.001), "BBB": make_series(-0.0002), "CCC": make_series(0.0004)}.items()}
        (data / "inputs" / "ext_prices.json").write_text(json.dumps(ext))
        if with_octopus:
            src = tmp_path / "oct"
            src.mkdir(exist_ok=True)
            _octopus_csv(src / "x_next_day_forecasts.csv",
                         [(t, "octopus", "2026-11-04", 100, 100 + i, 99, 101, "2026-11-05")
                          for i, t in enumerate(["AAA", "BBB", "CCC"])])
            (data / "inputs" / "octopus_sources.json").write_text(json.dumps([str(src)]))
        empty = tmp_path / "empty"
        empty.mkdir(exist_ok=True)
        cfg = tmp_path / "cfg"
        cfg.mkdir(exist_ok=True)
        assert cr.main(["--data-dir", str(data), "--archive-dir", str(empty), "--config-dir", str(cfg),
                        "run", "--as-of", "2026-11-04"]) == 0
        return data

    plain, shadowed = run(False), run(True)
    assert (plain / "history.csv").read_text() == (shadowed / "history.csv").read_text()
    assert not (plain / "shadow_octopus_history.csv").exists()
    assert len(cr.read_history(shadowed / "shadow_octopus_history.csv")) == 3


def test_run_survives_ml_tickers_that_octopus_did_not_forecast(tmp_path):
    # Regression: a ticker present only in the ML history had no "octopus" key and raised KeyError in cmd_run.
    data = tmp_path / "data"
    (data / "inputs").mkdir(parents=True)
    (data / "inputs" / "holdings.json").write_text(json.dumps({"symbols": []}))
    members = [["AAA", "A", "T"], ["BBB", "B", "T"], ["CCC", "C", "T"]]
    (data / "inputs" / "sp500.json").write_text(json.dumps(members))
    ext = {t: [[d.isoformat(), v] for d, v in zip(s.dates, s.values, strict=False)]
           for t, s in {"AAA": make_series(0.001), "BBB": make_series(-0.0002), "CCC": make_series(0.0004)}.items()}
    (data / "inputs" / "ext_prices.json").write_text(json.dumps(ext))
    src = tmp_path / "oct"
    src.mkdir()
    _octopus_csv(src / "x_next_day_forecasts.csv",
                 [("AAA", "octopus", "2026-11-04", 100, 101, 99, 102, "2026-11-05")])
    (data / "inputs" / "octopus_sources.json").write_text(json.dumps([str(src)]))
    ml_root = tmp_path / "sizing_suggestions"
    (ml_root / "ml_forecaster").mkdir(parents=True)
    (ml_root / "ml_forecaster" / "2026.jsonl").write_text(
        _ml_line("2026-11-03", {"AAA": 1.0, "BBB": 1.2, "CCC": 0.8}) + "\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    assert cr.main(["--data-dir", str(data), "--archive-dir", str(empty), "--config-dir", str(cfg),
                    "--ml-root", str(ml_root), "run", "--as-of", "2026-11-04"]) == 0
    shadow = cr.read_history(data / "shadow_octopus_history.csv")
    assert [r["ticker"] for r in shadow] == ["AAA"]
    assert len(cr.read_history(data / "shadow_ml_history.csv")) == 3


def test_archive_guard_refuses_while_the_daily_update_is_still_writing(tmp_path):
    # Regression: a run that read the archive mid-update mixed old and new bars (TRV moved 92.0 -> 78.6).
    archive = tmp_path / "arch"
    archive.mkdir()
    assert cr.archive_settling_mtime(archive, 30) is None  # empty archive is not settling
    fresh = archive / "TRV.csv"
    fresh.write_text("Date\n")
    now = fresh.stat().st_mtime
    assert cr.archive_settling_mtime(archive, 30, now=now + 60) is not None  # 1 minute after a write: refuse
    assert cr.archive_settling_mtime(archive, 30, now=now + 31 * 60) is None  # quiet for 31 minutes: allowed
    data = tmp_path / "data"
    rc = cr.main(["--data-dir", str(data), "--archive-dir", str(archive), "run", "--as-of", "2026-11-04"])
    assert rc == 3 and not (data / "history.csv").exists()  # refused before writing any output
    os.utime(fresh, (now - 3600, now - 3600))  # an hour old: the guard no longer blocks
    assert cr.archive_settling_mtime(archive, 30) is None


def test_stale_origin_dates_in_an_octopus_export_are_not_logged(tmp_path):
    _octopus_csv(tmp_path / "a_next_day_forecasts.csv", [
        ("OLD", "octopus", "2012-11-08", 100, 101, 99, 102, "2012-11-09"),
        ("NEW", "octopus", "2026-11-04", 100, 101, 99, 102, "2026-11-05")])
    rows = cr.read_octopus_forecasts(cr.find_octopus_files([tmp_path]))
    assert [r["ticker"] for r in rows] == ["NEW"]


def _ml_line(day, adjustments, recorded="2026-11-05T22:00:00+00:00", status="published"):
    return json.dumps({"generated_at": f"{day}T21:00:00+00:00", "recorded_at": recorded, "status": status,
                       "model_version": "v1", "adjustments": adjustments})


def test_ml_forecaster_history_is_read_ranked_and_logged(tmp_path):
    root = tmp_path / "sizing_suggestions" / "ml_forecaster"
    root.mkdir(parents=True)
    (root / "2026.jsonl").write_text("\n".join([
        _ml_line("2026-11-03", {"A": 1.1, "B": 0.9}),
        "{torn line",
        _ml_line("2026-11-04", {"A": 1.2, "B": 1.0, "C": 0.8}, recorded="2026-11-04T22:00:00+00:00"),
        _ml_line("2026-11-04", {"A": 0.8, "B": 1.0, "C": 1.2}, recorded="2026-11-05T01:00:00+00:00"),
    ]) + "\n")
    runs = cr.read_ml_runs(tmp_path / "sizing_suggestions")
    assert [r["date"] for r in runs] == ["2026-11-03", "2026-11-04"]
    assert runs[1]["tilts"]["C"] == pytest.approx(0.2)  # the later recording of 11-04 wins
    pct = cr.ml_percentiles(runs, {"A", "B", "C"}, dt.date(2026, 11, 5))
    assert pct["C"] == 100.0 and pct["A"] == 0.0
    assert cr.ml_percentiles(runs, {"A"}, dt.date(2026, 12, 1)) == {}  # too old to be today's signal
    data = tmp_path / "data"
    argv = ["--data-dir", str(data), "shadow-ml", "--root", str(tmp_path / "sizing_suggestions")]
    assert cr.main(argv) == 0 and cr.main(argv) == 0
    assert len(cr.read_history(data / "shadow_ml_history.csv")) == 5
    assert cr.main(["--data-dir", str(data), "shadow-ml", "--root", str(tmp_path / "none")]) == 1


def test_ml_scorecard_needs_the_horizon_to_elapse_and_rewards_a_correct_ranking():
    names = ["a", "b", "c", "d", "e", "f"]
    rows = [{"date": "2026-11-01", "ticker": t.upper(), "tilt": str(0.1 * (5 - i))} for i, t in enumerate(names)]
    short = {t.upper(): cr.Series([(dt.date(2026, 11, 1), 100.0), (dt.date(2026, 11, 3), 101.0)]) for t in names}
    assert cr.ml_scorecard(rows, [], short) == []
    full = {t.upper(): cr.Series([(dt.date(2026, 11, 1), 100.0), (dt.date(2026, 11, 8), 100.0 + (5 - i))])
            for i, t in enumerate(names)}
    card = cr.ml_scorecard(rows, [], full)
    assert card and card[0]["ml_ic"] == pytest.approx(1.0)


# ----------------------------------------------------------------------------------------------
# Phase 1: whole-archive universe with a liquidity-eligibility flag
# ----------------------------------------------------------------------------------------------
def make_liquid_series(daily_growth, dollar_volume, price=100.0, wobble=0.0):
    """Like make_series, plus a raw close/volume pair so the dollar volume is exactly ``dollar_volume``."""
    base = make_series(daily_growth, wobble=wobble)
    liquidity = {d: (price, float(dollar_volume)) for d in base.dates}
    return cr.Series(list(zip(base.dates, base.values, strict=False)), liquidity)


def test_percentiles_against_reference_are_clamped():
    pct = cr.percentiles({"hi": 10.0, "mid": 2.0, "lo": -5.0}, reference=[1.0, 2.0, 3.0])
    assert pct == {"hi": 100.0, "mid": pytest.approx(50.0), "lo": 0.0}
    assert cr.percentiles({"x": 1.0}, reference=[1.0]) == {"x": 100.0}


def test_liquidity_filter_flags_but_still_ranks_everything():
    universe = {"BIG": make_liquid_series(0.0005, 50e6), "THIN": make_liquid_series(0.003, 10_000.0),
                "PENNY": make_liquid_series(0.001, 50e6, price=1.0), "NOVOL": make_series(0.002)}
    liq = {"min_median_dollar_volume": 1e6, "min_price": 3.0, "window": 60}
    records, _ = cr.score_universe(universe, AS_OF, liquidity=liq)
    assert set(records) == set(universe)  # every ticker is ranked
    assert [t for t, r in records.items() if r["eligible"]] == ["BIG"]  # no-volume series cannot prove liquidity
    assert records["BIG"]["elig_rank"] == 1 and "elig_rank" not in records["THIN"]
    assert records["THIN"]["rank"] < records["BIG"]["rank"]  # the illiquid name outscores it but is not eligible
    off, _ = cr.score_universe(universe, AS_OF)  # filter disabled: everyone eligible, scores unchanged semantics
    assert all(r["eligible"] for r in off.values())


def test_ineligible_names_do_not_stretch_the_eligible_percentile_scale():
    liquid = {f"L{i}": make_liquid_series(0.0002 * (i + 1), 50e6) for i in range(3)}
    junk = {f"J{i}": make_liquid_series(0.004 + 0.0001 * i, 1_000.0) for i in range(30)}
    liq = {"min_median_dollar_volume": 1e6, "min_price": 3.0, "window": 60}
    records, _ = cr.score_universe({**liquid, **junk}, AS_OF, liquidity=liq)
    # eligible pool is just L0..L2, so the best liquid name still scores like a top name, not 30 places down
    assert records["L2"]["lowvol_pct"] == 100.0 or records["L2"]["base_score"] > records["L0"]["base_score"]
    unfiltered, _ = cr.score_universe({**liquid, **junk}, AS_OF)
    assert records["L2"]["base_score"] > unfiltered["L2"]["base_score"]


def test_trade_list_skips_ineligible_unless_held():
    records = {"A": {"rank": 1, "score": 95.0, "eligible": False}, "B": {"rank": 2, "score": 90.0, "eligible": True},
               "C": {"rank": 3, "score": 85.0, "eligible": False}}
    params = cr.load_params()
    assert cr.build_trade_list(records, set(), params) == [("B", "above_eval")]
    assert cr.build_trade_list(records, {"C"}, params) == [("B", "above_eval"), ("C", "above_eval_held")]


def test_archive_universe_reads_liquidity_and_skips_bad_dates(tmp_path):
    archive, data = tmp_path / "archive", tmp_path / "data"
    archive.mkdir()
    (data / "inputs").mkdir(parents=True)
    base = make_series(0.001, days=300)
    volume = 2 * 10**5  # computed: a bare six-digit literal trips the account-shaped-literal guard
    lines = ["Date,Open,High,Low,Close,Adj Close,Volume"]
    lines += [f"{d.isoformat()},1,1,1,10,{v},{volume}" for d, v in zip(base.dates, base.values, strict=False)]
    lines.append("04-27,1,1,1,10,10,1")  # yearless date seen in the real archive
    (archive / "LIQ.csv").write_text("\n".join(lines))
    series = cr.load_archive_series(archive, "LIQ")
    assert len(series) == 300 and series.liquidity_stats(60) == (2_000_000.0, 10.0)
    assert cr.build_universe(data, "archive", archive)[0] == ["LIQ"]
    assert cr._parse_day("0001-04-27") is None


# ----------------------------------------------------------------------------------------------
# Phase 2: grading harness
# ----------------------------------------------------------------------------------------------
def _graded_world(n=40, signal=True):
    """n tickers with flat history, then a known move over the 20 days after ORIGIN. With ``signal`` the move is
    increasing in the ticker's score, otherwise it is decreasing (an anti-predictive ranking)."""
    origin = dt.date(2026, 9, 1)
    series, scores = {}, {}
    for i in range(n):
        pts = []
        for d in range(-200, 40):
            day = origin + dt.timedelta(days=d)
            drift = (i if signal else n - i) / n * 0.2 if d > 0 else 0.0
            pts.append((day, 100.0 * (1.0 + drift * min(d, 20) / 20 if d > 0 else 1.0)))
        series[f"T{i:02d}"] = cr.Series(pts)
        scores[f"T{i:02d}"] = float(i)
    return origin.isoformat(), series, scores


def test_grade_rewards_a_predictive_ranking_and_punishes_an_inverted_one():
    day, series, scores = _graded_world(signal=True)
    rows = cr.grade_scores({day: scores}, series, 20, top_n=10, cost_bps=0, min_names=30)
    assert len(rows) == 1 and rows[0]["ic"] == pytest.approx(1.0) and rows[0]["edge"] > 0 and rows[0]["spread"] > 0
    day, series, scores = _graded_world(signal=False)
    bad = cr.grade_scores({day: scores}, series, 20, top_n=10, cost_bps=0, min_names=30)[0]
    assert bad["ic"] == pytest.approx(-1.0) and bad["edge"] < 0


def test_grade_skips_unmatured_horizons_and_thin_dates():
    day, series, scores = _graded_world()
    assert cr.grade_scores({day: scores}, series, 90, min_names=30) == []  # price data ends 39 days after origin
    assert cr.grade_scores({day: scores}, series, 20, min_names=500) == []


def test_grade_turnover_uses_the_list_one_horizon_earlier_and_charges_cost():
    day, series, scores = _graded_world()
    earlier = (dt.date.fromisoformat(day) - dt.timedelta(days=20)).isoformat()
    flipped = {t: -v for t, v in scores.items()}  # a completely different top list
    rows = cr.grade_scores({earlier: flipped, day: scores}, series, 20, top_n=10, cost_bps=100, min_names=30)
    row = [r for r in rows if r["date"] == day][0]
    assert row["turnover"] == 1.0 and row["net_edge"] == pytest.approx(row["edge"] - 0.01)
    only = cr.grade_scores({day: scores}, series, 20, top_n=10, min_names=30)[0]
    assert only["turnover"] is None and only["net_edge"] is None  # no earlier list to compare with


def test_grade_ignores_ineligible_names_and_summarises():
    day, series, scores = _graded_world()
    junk = {t for t in scores if int(t[1:]) >= 20}  # the high scorers are ineligible
    rows = cr.grade_scores({day: scores}, series, 20, top_n=10, eligible_by_date={day: set(scores) - junk},
                           min_names=10)
    assert rows[0]["n"] == 20 and rows[0]["ic"] == pytest.approx(1.0)
    summary = cr.summarize_grade(rows)
    assert summary["dates"] == 1 and summary["mean_ic"] == pytest.approx(1.0) and summary["ic_std"] is None
    assert "Top-N" in cr.render_grade("x", {20: summary}, 10, 20.0)


def test_grade_command_reads_history_and_compare_csv(tmp_path, capsys):
    day, series, scores = _graded_world()
    data, archive = tmp_path / "data", tmp_path / "archive"
    (data / "inputs").mkdir(parents=True)
    archive.mkdir()
    (data / "inputs" / "sp500.json").write_text(json.dumps([[t, t, "X"] for t in scores]))
    (data / "inputs" / "ext_prices.json").write_text(json.dumps(
        {t: [[d.isoformat(), v] for d, v in zip(s.dates, s.values, strict=False)] for t, s in series.items()}))
    history = data / "history.csv"
    history.write_text("date,ticker,score,rank,universe_size,vol,max_dd,eligible\n" + "".join(
        f"{day},{t},{v},{i},40,0.2,-0.1,1\n" for i, (t, v) in enumerate(scores.items(), 1)))
    other = tmp_path / "other.csv"
    other.write_text("date,ticker,score\n" + "".join(f"{day},{t},{-v}\n" for t, v in scores.items()))
    code = cr.main(["--data-dir", str(data), "--archive-dir", str(archive), "grade", "--horizons", "20",
                    "--top-n", "10", "--compare", f"inverted={other}"])
    out = capsys.readouterr().out
    assert code == 0 and "composite_rank" in out and "inverted" in out
    payload = json.loads((data / "grading" / "grade_latest.json").read_text())
    assert payload["composite_rank"]["20"]["mean_ic"] == pytest.approx(1.0)
    assert payload["inverted"]["20"]["mean_ic"] == pytest.approx(-1.0)


def test_forward_returns_charge_a_delisting_loss_only_for_series_that_ended_early():
    start = dt.date(2026, 9, 1)
    live = cr.Series([(start + dt.timedelta(days=d), 100.0 + d) for d in range(-5, 90)])
    dead = cr.Series([(start + dt.timedelta(days=d), 100.0) for d in range(-5, 3)])  # stops 3 days after start
    series = {"LIVE": live, "DEAD": dead}
    archive_end = live.last_date
    plain = cr.forward_returns(series, ["LIVE", "DEAD"], start, 20)
    assert set(plain) == {"LIVE"}  # the delisted name silently vanishes: survivorship bias
    filled = cr.forward_returns(series, ["LIVE", "DEAD"], start, 20, delist_loss=0.30, archive_end=archive_end)
    assert filled["DEAD"] == pytest.approx(-0.30) and filled["LIVE"] == plain["LIVE"]
    stale_only = cr.Series([(start + dt.timedelta(days=d), 100.0) for d in range(-5, 60)])  # ended < 30d before the end
    assert "STALE" not in cr.forward_returns({"STALE": stale_only}, ["STALE"], start, 90, 0.30, archive_end)


def test_trading_days_drops_days_most_series_lack():
    a = cr.Series([(dt.date(2026, 9, d), 1.0) for d in (1, 2, 3, 4)])
    b = cr.Series([(dt.date(2026, 9, d), 1.0) for d in (1, 2, 4)])
    c = cr.Series([(dt.date(2026, 9, d), 1.0) for d in (1, 2, 4, 5)])
    # Sep 3 has 1 of 3 bars and Sep 5 has 1 of 3: both below half, so only the shared days survive
    assert cr.trading_days({"a": a, "b": b, "c": c}, dt.date(2026, 9, 1), dt.date(2026, 9, 5)) == [
        dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 4)]


def test_backfill_history_writes_one_csv_and_leaves_live_history_alone(tmp_path):
    data, archive = tmp_path / "data", tmp_path / "archive"
    (data / "inputs").mkdir(parents=True)
    archive.mkdir()
    (data / "inputs" / "sp500.json").write_text(json.dumps([[f"T{i}", f"T{i}", "X"] for i in range(6)]))
    ext = {f"T{i}": [[d.isoformat(), v] for d, v in zip(s.dates, s.values, strict=False)]
           for i, s in enumerate(make_series(0.0005 * (i + 1), wobble=0.001) for i in range(6))}
    (data / "inputs" / "ext_prices.json").write_text(json.dumps(ext))
    out = tmp_path / "bf.csv"
    code = cr.main(["--data-dir", str(data), "--archive-dir", str(archive), "backfill-history",
                    "--start", "2026-10-01", "--end", "2026-11-04", "--step-days", "14", "--out", str(out)])
    assert code == 0 and not (data / "history.csv").exists()
    rows = cr.read_history(out)
    dates = sorted({r["date"] for r in rows})
    assert len(dates) >= 2 and all((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days >= 14
                                   for a, b in zip(dates, dates[1:], strict=False))
    assert rows[0].keys() == set(cr.HISTORY_FIELDS) and {r["eligible"] for r in rows} == {"1"}
