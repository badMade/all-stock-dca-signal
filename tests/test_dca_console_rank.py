"""Unit tests for dca_console_rank.py (composite-rank page reader; read-only, tmp_path data only)."""

import json
from pathlib import Path

from dca_console_rank import _band, build_rank_snapshot

RANKED_HEADER = "rank,ticker,company,sector,held,score,base_score,lowvol_pct,lowdd_pct,vol,max_dd,ret_1y,ret_1m,ret_1w,ret_3m,ret_6m,ret_5y\n"


def _write_run(root: Path, day: str, order: list[str], threshold: float = 80.0, held: frozenset = frozenset()):
    run = root / "artifacts" / "composite_rank" / "runs" / day
    run.mkdir(parents=True)
    lines = [RANKED_HEADER]
    for position, ticker in enumerate(order, 1):
        score = 95.0 - position * 5
        lines.append(
            f"{position},{ticker},{ticker} Inc,Tech,{int(ticker in held)},{score},50,50,50,0.2,-0.1,0.3,0.1,0.02,0.05,0.15,\n"
        )
    (run / "ranked_all.csv").write_text("".join(lines))
    above = sum(1 for p in range(1, len(order) + 1) if 95.0 - p * 5 > threshold)
    (run / "summary.json").write_text(json.dumps({"universe": "sp500", "scored": len(order), "threshold": threshold,
                                                  "above_threshold": above}))
    (run / "data_gaps.json").write_text(json.dumps({"no_price_history": ["ZZZ"], "not_scored_or_stale": []}))


def _write_history(root: Path, days_orders: dict[str, list[str]]):
    rows = ["date,ticker,held,score,rank,universe_size,vol,max_dd\n"]
    for day, order in days_orders.items():
        for position, ticker in enumerate(order, 1):
            rows.append(f"{day},{ticker},0,{95.0 - position * 5},{position},{len(order)},0.2,-0.1\n")
    (root / "artifacts" / "composite_rank" / "history.csv").write_text("".join(rows))


def test_no_data_is_reported_not_raised(tmp_path: Path):
    snapshot = build_rank_snapshot(tmp_path)
    assert snapshot["available"] is False and "daily-composite-rank" in snapshot["message"]


def test_snapshot_shows_changes_and_movement(tmp_path: Path):
    _write_run(tmp_path, "2026-11-03", ["A", "B", "C", "D"])
    _write_run(tmp_path, "2026-11-04", ["B", "A", "D", "C"], held=frozenset({"A"}))
    _write_history(tmp_path, {"2026-11-03": ["A", "B", "C", "D"], "2026-11-04": ["B", "A", "D", "C"]})
    snap = build_rank_snapshot(tmp_path)
    assert snap["available"] and snap["as_of"] == "2026-11-04" and snap["previous_run"] == "2026-11-03"
    assert snap["scored"] == 4 and snap["above_threshold"] == 2  # scores 90 and 85 clear 80
    assert [r["ticker"] for r in snap["evaluation"]] == ["B", "A"]
    assert snap["held_above_threshold"] == 1
    assert snap["entered"] == [] and snap["dropped"] == []  # same two names above 80 both days
    b = next(r for r in snap["ranked"] if r["ticker"] == "B")
    assert b["rank_change"] == 1  # climbed from 2 to 1
    assert snap["no_price_history"] == ["ZZZ"]
    movement = snap["movement"]
    assert movement["bands"][0]["band"] == "1-10"
    assert movement["climbers"][0]["ticker"] in {"B", "D"} and movement["climbers"][0]["change_1d"] == 1


def test_entered_and_dropped_names(tmp_path: Path):
    _write_run(tmp_path, "2026-11-03", ["A", "B", "C", "D"])
    _write_run(tmp_path, "2026-11-04", ["C", "D", "A", "B"])
    _write_history(tmp_path, {"2026-11-03": ["A", "B", "C", "D"], "2026-11-04": ["C", "D", "A", "B"]})
    snap = build_rank_snapshot(tmp_path)
    assert snap["entered"] == ["C", "D"] and snap["dropped"] == ["A", "B"]


def test_band_labels():
    assert _band(1) == "1-10" and _band(11) == "11-25" and _band(250) == "201+"


def test_corrupt_inputs_become_warnings_not_errors(tmp_path: Path):
    _write_run(tmp_path, "2026-11-04", ["A", "B"])
    (tmp_path / "artifacts" / "composite_rank" / "runs" / "2026-11-04" / "summary.json").write_text("{not json")
    (tmp_path / "artifacts" / "composite_rank" / "history.csv").write_text("date,ticker\n")
    snap = build_rank_snapshot(tmp_path)
    assert snap["available"] and snap["threshold"] == 80.0
    assert any("corrupt" in w for w in snap["warnings"])


def test_trade_list_and_shadow_views(tmp_path: Path):
    _write_run(tmp_path, "2026-11-04", ["A", "B", "C", "D"], held=frozenset({"A"}))
    root = tmp_path / "artifacts" / "composite_rank"
    (root / "latest_trade_list.csv").write_text(
        "date,ticker,rank,score,reason,params_version\n2026-11-04,A,2,85.0,above_eval_held,v1\n")
    (root / "trade_list_history.csv").write_text(
        "date,ticker,rank,score,reason,params_version\n2026-11-03,A,1,90,above_eval,v1\n"
        "2026-11-04,A,2,85,above_eval_held,v1\n2026-11-04,B,1,90,above_eval,v1\n")
    (root / "latest_tickers_proposal.md").write_text("# Proposed ticker change\n")
    (root / "shadow_octopus_history.csv").write_text(
        "origin_date,ticker,exp_ret,p10_ret,p90_ret,expected_target_date,fwd_pct,model\n"
        "2026-11-04,A,0.004,-0.01,0.02,2026-11-05,90,octopus\n"
        "2026-11-04,B,-0.002,-0.02,0.01,2026-11-05,40,octopus\n"
        "2026-11-04,ZZZZ,0.05,-0.1,0.2,2026-11-05,100,octopus\n")  # micro-cap outside the ranked universe
    (root / "shadow_ml_history.csv").write_text(
        "date,ticker,tilt,fwd_pct,status,model_version\n"
        "2026-11-04,A,0.2,50,blocked_regression,m\n2026-11-04,B,-0.1,50,blocked_regression,m\n")
    snap = build_rank_snapshot(tmp_path)
    trade = snap["trade"]
    assert trade["latest"][0]["held"] is True and trade["params_version"] == "v1"
    assert trade["history"][0] == {"date": "2026-11-04", "names": 2, "added": 1, "removed": 0}
    assert trade["proposal_markdown"].startswith("# Proposed")
    octopus = snap["shadow"]["octopus"]
    assert [r["ticker"] for r in octopus["top"]] == ["A", "B"]  # ZZZZ is not a ranked name
    assert octopus["dates"][0]["tickers"] == 3 and octopus["weight"] == 0
    ml = snap["shadow"]["ml"]
    assert ml["top"][0]["ticker"] == "A" and ml["dates"][0]["status"] == {"blocked_regression": 2}


def test_missing_shadow_files_are_not_an_error(tmp_path: Path):
    _write_run(tmp_path, "2026-11-04", ["A", "B"])
    snap = build_rank_snapshot(tmp_path)
    assert snap["trade"]["latest"] == [] and snap["shadow"]["octopus"]["available"] is False
    assert snap["shadow"]["ml"]["available"] is False
