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
