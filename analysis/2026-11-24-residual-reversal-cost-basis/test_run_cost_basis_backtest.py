"""Offline checks of the ladder replay and the archive loader. analysis/ is outside the root
pytest sweep, so run by hand from the repo root:

    PYTHONPATH=.:dca-sizing-signals python -m pytest analysis/2026-11-24-residual-reversal-cost-basis -q
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rr_cb_run", HERE / "run_cost_basis_backtest.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)

TIERS, FULL, REDUCED = run.load_ladder(HERE.parents[1] / "config" / "sizing_ladder.json")


def _bars(*rows):
    return [(date(2026, 1, d), c, low) for d, c, low in rows]


def test_ladder_matches_the_live_rules():
    bars = {"A": _bars((5, 100.0, 99.0),
                       (6, 99.5, 99.4),    # -0.5%, above the 99.0 low -> $1 * 1.5x
                       (7, 95.0, 94.0),    # -4.52% and below the 99.4 low -> $2 * 4.5x
                       (8, 96.0, 95.0),    # up -> no buy
                       (9, 95.99, 95.5))}  # -0.0104%, under the 0.1% floor -> no buy
    out = run.ladder_baseline(bars, date(2026, 1, 1), date(2026, 1, 31), TIERS, FULL, REDUCED)
    assert out == {date(2026, 1, 6): {"A": 1.5}, date(2026, 1, 7): {"A": 9.0}}


def test_ladder_reads_no_later_price_and_honours_the_window():
    bars = {"A": _bars((5, 100.0, 99.0), (6, 98.0, 97.0), (7, 90.0, 80.0))}
    full = run.ladder_baseline(bars, date(2026, 1, 1), date(2026, 1, 31), TIERS, FULL, REDUCED)
    cut = run.ladder_baseline({"A": bars["A"][:2]}, date(2026, 1, 1), date(2026, 1, 31), TIERS, FULL, REDUCED)
    assert full[date(2026, 1, 6)] == cut[date(2026, 1, 6)]
    assert run.ladder_baseline(bars, date(2026, 1, 7), date(2026, 1, 7), TIERS, FULL, REDUCED).keys() == {
        date(2026, 1, 7)}


def test_load_bars_drops_bad_rows(tmp_path):
    (tmp_path / "A.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n"
        "0001-04-27,1,1,1,1,1,1,0,0\n"          # yearless row parsed to year 1
        "2026-01-05,1,1,9.5,10,10,1,0,0\n"
        "2026-01-06,1,1,,,,,0,0\n"             # today's partial bar
        "2026-01-07,1,1,9.0,-1,1,1,0,0\n"
        "2026-01-08,1,1,9.0,9.5,9.5,1,0,0\n")
    assert run.load_bars(tmp_path, "A", date(2020, 1, 1), date(2026, 12, 31)) == [
        (date(2026, 1, 5), 10.0, 9.5), (date(2026, 1, 8), 9.5, 9.0)]
    assert run.load_bars(tmp_path, "MISSING", date(2020, 1, 1), date(2026, 12, 31)) == []
