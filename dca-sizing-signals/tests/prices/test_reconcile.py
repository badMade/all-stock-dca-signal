"""Reconciliation of Massive against yahoo_legacy, and the splice guard.

Expected behaviour (PLAN-price-source.md §4):
- window = the last min(250, overlap) overlapping sessions; under 60 is "insufficient";
- Yahoo Close is split-adjusted as of its download, so Massive raw closes are split-adjusted
  to the Yahoo file's last session before comparing;
- per-session close diff: <= 0.1% pass, 0.1%-1% flag, > 1% block; the symbol takes its
  worst session; volume off by more than 10% raises volume_flag and never blocks alone;
- results are appended with source='massive', reference_source='yahoo_legacy';
- a spliced series is refused unless a passing reconciliation ends at the splice date.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from market_data_ingest.prices import reconcile
from market_data_ingest.prices.errors import SpliceNotValidatedError
from market_data_ingest.prices.legacy_yahoo import import_csv
from market_data_ingest.prices.models import CorporateAction, DailyBar

from .conftest import utc, weekdays

SESSIONS = weekdays(date(2023, 1, 2), date(2025, 6, 30))
NOW = utc(2026, 10, 18, 12)
YAHOO_HEADER = "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n"


def write_yahoo(tmp_path: Path, symbol: str, rows: list[tuple[date, float, float]]) -> Path:
    """rows: (session, close, volume). Adj Close == Close (no dividends in these cases)."""
    path = tmp_path / f"{symbol}.csv"
    body = "".join(f"{d.isoformat()},{c},{c * 1.01},{c * 0.99},{c},{c},{v},0.0,0.0\n" for d, c, v in rows)
    path.write_text(YAHOO_HEADER + body)
    return path


def massive_bars(symbol: str, rows: list[tuple[date, float, float]]) -> list[DailyBar]:
    return [DailyBar(source="massive", symbol=symbol, session=d, open=c, high=c * 1.01, low=c * 0.99, close=c,
                     volume=v, vwap=None, trades=None) for d, c, v in rows]


def load(store, staging_dir, tmp_path, symbol, massive_rows, yahoo_rows, actions=()):
    store.ingest_daily_bars(massive_bars(symbol, massive_rows), run_id="m", as_of=NOW, staging_dir=staging_dir)
    if actions:
        store.ingest_corporate_actions(list(actions), run_id="ca", as_of=NOW, staging_dir=staging_dir)
    import_csv(store, write_yahoo(tmp_path, symbol, yahoo_rows), run_id="y", now=NOW)


def identical(n: int, start: int = 0):
    return [(d, 100.0 + i, 1_000_000.0) for i, d in enumerate(SESSIONS[start:start + n])]


def run(store, symbol):
    return reconcile.reconcile_symbol(store, symbol, run_id="rec-1", now=NOW)


@pytest.mark.parametrize(
    ("diff", "tier"),
    [(0.0, "pass"), (0.0009, "pass"), (0.001, "pass"), (0.0011, "flag"), (0.005, "flag"), (0.01, "flag"),
     (0.0101, "block"), (0.5, "block")],
)
def test_close_diff_tiers_and_their_boundaries(diff, tier):
    assert reconcile.classify_close_diff(diff) == tier


def test_identical_series_pass_and_are_recorded(store, staging_dir, tmp_path):
    rows = identical(80)
    load(store, staging_dir, tmp_path, "AAA", rows, rows)

    result = run(store, "AAA")

    assert result.status == "pass"
    assert result.sessions == 80
    assert result.volume_flag is False
    [recorded] = store.reconciliation_results("AAA")
    assert (recorded["status"], recorded["source"], recorded["reference_source"]) == ("pass", "massive", "yahoo_legacy")
    assert recorded["run_id"] == "rec-1"


def test_fewer_than_60_overlapping_sessions_is_insufficient(store, staging_dir, tmp_path):
    rows = identical(59)
    load(store, staging_dir, tmp_path, "AAA", rows, rows)

    assert run(store, "AAA").status == "insufficient"


def test_window_is_capped_at_the_last_250_overlapping_sessions(store, staging_dir, tmp_path):
    rows = identical(300)
    load(store, staging_dir, tmp_path, "AAA", rows, rows)

    result = run(store, "AAA")
    assert result.sessions == 250
    assert result.window_start == rows[50][0]
    assert result.window_end == rows[-1][0]


def test_a_mismatch_outside_the_window_does_not_count(store, staging_dir, tmp_path):
    massive = identical(300)
    yahoo = list(massive)
    d, c, v = yahoo[10]
    yahoo[10] = (d, c * 1.05, v)  # 5% off, but 290 sessions before the end
    load(store, staging_dir, tmp_path, "AAA", massive, yahoo)

    assert run(store, "AAA").status == "pass"


@pytest.mark.parametrize(("bump", "status"), [(1.005, "flag"), (1.02, "block")])
def test_worst_session_sets_the_symbol_tier(store, staging_dir, tmp_path, bump, status):
    massive = identical(80)
    yahoo = list(massive)
    d, c, v = yahoo[40]
    yahoo[40] = (d, c * bump, v)
    load(store, staging_dir, tmp_path, "AAA", massive, yahoo)

    result = run(store, "AAA")
    assert result.status == status
    assert result.worst_close_diff_pct == pytest.approx(abs(1 - 1 / bump) * 100, rel=1e-3)


def test_volume_off_by_more_than_10_percent_flags_but_never_blocks(store, staging_dir, tmp_path):
    massive = identical(80)
    yahoo = list(massive)
    d, c, v = yahoo[5]
    yahoo[5] = (d, c, v * 1.15)
    load(store, staging_dir, tmp_path, "AAA", massive, yahoo)

    result = run(store, "AAA")
    assert result.volume_flag is True
    assert result.status == "pass", "close agreed, so the close tier stands"


def test_massive_raw_is_split_adjusted_before_comparing_with_yahoo_close(store, staging_dir, tmp_path):
    sessions = SESSIONS[:80]
    ex = sessions[40]
    # Yahoo shows the split-adjusted history; Massive stores raw (10x price, 1/10 volume before ex).
    yahoo = [(d, 100.0 + i, 10_000_000.0) for i, d in enumerate(sessions)]
    massive = [(d, (100.0 + i) * (10 if d < ex else 1), 1_000_000.0 if d < ex else 10_000_000.0)
               for i, d in enumerate(sessions)]
    nvda_split = CorporateAction(source="massive", action_type="forward_split", symbol="SPLT", ex_date=ex,
                                 split_from=1, split_to=10)
    load(store, staging_dir, tmp_path, "SPLT", massive, yahoo, actions=[nvda_split])

    result = run(store, "SPLT")
    assert result.status == "pass"
    assert result.volume_flag is False


def test_an_unrecorded_split_shows_up_as_a_block_not_a_pass(store, staging_dir, tmp_path):
    sessions = SESSIONS[:80]
    ex = sessions[40]
    yahoo = [(d, 100.0 + i, 1_000_000.0) for i, d in enumerate(sessions)]
    massive = [(d, (100.0 + i) * (10 if d < ex else 1), 1_000_000.0) for i, d in enumerate(sessions)]
    load(store, staging_dir, tmp_path, "SPLT", massive, yahoo)  # no corporate action ingested

    assert run(store, "SPLT").status == "block"


# --- splice guard ---------------------------------------------------------------------------


def test_splice_without_any_reconciliation_is_refused(store, staging_dir, tmp_path):
    rows = identical(80)
    load(store, staging_dir, tmp_path, "AAA", rows, rows)

    with pytest.raises(SpliceNotValidatedError):
        reconcile.spliced_adjusted_series(store, "AAA", rows[-1][0], as_of_date=rows[-1][0])


@pytest.mark.parametrize("bump", [1.005, 1.02], ids=["flag", "block"])
def test_splice_after_a_non_passing_reconciliation_is_refused(store, staging_dir, tmp_path, bump):
    massive = identical(80)
    yahoo = list(massive)
    d, c, v = yahoo[40]
    yahoo[40] = (d, c * bump, v)
    load(store, staging_dir, tmp_path, "AAA", massive, yahoo)
    run(store, "AAA")

    with pytest.raises(SpliceNotValidatedError):
        reconcile.spliced_adjusted_series(store, "AAA", massive[-1][0], as_of_date=massive[-1][0])


def test_splice_at_a_date_other_than_the_validated_window_end_is_refused(store, staging_dir, tmp_path):
    rows = identical(80)
    load(store, staging_dir, tmp_path, "AAA", rows, rows)
    run(store, "AAA")

    with pytest.raises(SpliceNotValidatedError):
        reconcile.spliced_adjusted_series(store, "AAA", rows[60][0], as_of_date=rows[-1][0])


def test_validated_splice_uses_yahoo_before_and_massive_from_the_splice_date(store, staging_dir, tmp_path):
    yahoo = identical(80)
    massive = identical(85)  # Massive keeps going after the Yahoo file ends
    load(store, staging_dir, tmp_path, "AAA", massive, yahoo)
    result = run(store, "AAA")
    splice = result.window_end
    assert splice == yahoo[-1][0]

    series = reconcile.spliced_adjusted_series(store, "AAA", splice, as_of_date=massive[-1][0])

    sessions = [b.session for b in series]
    assert sessions == sorted(set(sessions)), "no session appears twice"
    assert sessions == [d for d, _, _ in massive]
    assert {b.source for b in series if b.session < splice} == {"yahoo_legacy"}
    assert {b.source for b in series if b.session >= splice} == {"massive"}
