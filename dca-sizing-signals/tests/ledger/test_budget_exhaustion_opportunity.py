"""Opportunity sizing for the MDP cash-pacing and absorbing-tier-chain ideas (read-only).

Question this answers before anything gets built: how often did a LATER scheduled slot see a
buyable dip after the day's budget was already too spent to fund the buy the ladder would have
made? If that almost never happens, an MDP that reserves cash for later slots has nothing to
save, and both ideas drop down the roadmap (claude/markov-extensions.md).

Revision 2 (2026-11-23). Revision 1 priced every deep dip as a flat "$2 base x 2x = $4 Tier B"
buy. The repo's ladder is not that: config/sizing_ladder.json is a log-scaled 1x-8x ladder on
``fraction_down_vs_previous_close`` (the largest ``min_drop_pct`` <= the drop wins; under 0.1%
is no buy), times a $2.00 base when the price is strictly below the prior low, or a $1.00 base when
it is below the prior close only (dca_ladder_core.select_base_usd). So the exhaustion
threshold is now the ladder's own planned dollars for that quote, and the operator's answers
are folded in: count BOTH dip kinds (below_low and below_close_only) and run at ALL FOUR
daily caps.

Proposed API (``opportunity.py``):

    ladder = load_global_ladder()                       # config/sizing_ladder.json
    report = budget_exhaustion_opportunity(ledger, known_at=..., daily_cap_usd=180.0, ladder=ladder)
    reports = budget_exhaustion_opportunity_all_caps(ledger, known_at=...)   # one per budgets.json tier

* "Exhausted at a slot" means ``daily_cap_usd - spent_before < planned_usd``, where
  ``spent_before`` sums SCHEDULED fills for that session with ``filled_at`` strictly before the
  quote's ``ts``, across all tickers. Manual fills never count (docs/run_classification.md).
* The GLOBAL ladder only: per-ticker calibrations, the ML nudge and the kill-switch cap are not
  replayed. The report is a floor-ish estimate of the opportunity, not a replay of live sizing.
* Point in time: only fills, quotes and bars with ``as_of <= known_at`` are visible.
* Fail closed: a quote with no prior-session bar is skipped and counted, never guessed.
  Bad parameters raise ``ValueError``. The function never writes.

Fixtures: the ladder and caps are the repo's REAL config files (read, never copied), so a config
change that moves an expected number fails loudly here. Ledger rows are synthetic and
pencil-checkable, per conftest.py: ZZZA's prior-session bar (SESSIONS[0], close 10.00) has low
9.90. The quote session is SESSIONS[1] = 2026-03-03 (EST, UTC-5), so 13:30 ET = 18:30 UTC and
09:35 ET = 14:35 UTC.

Pencil math against config/sizing_ladder.json (asserted in test_fixture_ladder_math_matches_config):
  price 9.50: drop 5.0% -> tier [4.5, 5.5) -> 4.5x; below low 9.90 -> $2 base -> planned $9.00
  price 9.97: drop 0.3% -> tier [0.1, 0.5) -> 1.0x; above low, below close -> $1 base -> $1.00
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_data_ingest.prices.models import Quote

from .conftest import FIRST_SEEN, LATER, SESSIONS, bar, fill, ingest_bars, ingest_calendar, utc

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
D1 = SESSIONS[1]
KNOW_ALL = utc(2026, 6, 1)
CAP = 180.0
SLOT_1330 = datetime(2026, 3, 3, 18, 30, tzinfo=timezone.utc)
FILL_0935 = datetime(2026, 3, 3, 14, 35, tzinfo=timezone.utc)
FILL_1530 = datetime(2026, 3, 3, 20, 30, tzinfo=timezone.utc)
DEEP = 9.50  # planned $9.00, below_low
SHALLOW = 9.97  # planned $1.00, below_close_only


def _mod():
    from market_data_ingest.ledger import opportunity

    return opportunity


def _run(ledger, **kwargs):
    m = _mod()
    kwargs.setdefault("known_at", KNOW_ALL)
    kwargs.setdefault("daily_cap_usd", CAP)
    kwargs.setdefault("ladder", m.load_global_ladder(REPO_CONFIG))
    return m.budget_exhaustion_opportunity(ledger, **kwargs)


def _fill(order_id, *, notional, filled_at=FILL_0935, run_kind="scheduled", slot="09:35", symbol="ZZZA"):
    row = fill(order_id, run_kind=run_kind, symbol=symbol, session=D1, notional=notional, slot=slot)
    row["filled_at"] = filled_at
    return row


def _setup(ledger, staging_dir, *, fills, quote_price=DEEP, quote_as_of=FIRST_SEEN, with_prior_bar=True):
    ingest_calendar(ledger)
    if with_prior_bar:
        ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[0], 10.00)])
    if fills:
        ledger.ingest_fills(fills, source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    quote = Quote(symbol="ZZZA", price=quote_price, ts=SLOT_1330, feed="iex", as_of=quote_as_of)
    ledger.prices.insert_intraday_quote(quote, slot="13:30", delayed=False, run_id="q1")


# --- the ladder is the repo's, not a copy ----------------------------------------------------------


def test_fixture_ladder_math_matches_config():
    ladder = _mod().load_global_ladder(REPO_CONFIG)
    assert ladder.planned_usd(DEEP, prior_close=10.00, prior_low=9.90) == ("below_low", pytest.approx(9.00))
    assert ladder.planned_usd(SHALLOW, prior_close=10.00, prior_low=9.90) == (
        "below_close_only", pytest.approx(1.00))


def test_ladder_no_buy_cases():
    ladder = _mod().load_global_ladder(REPO_CONFIG)
    assert ladder.planned_usd(10.00, prior_close=10.00, prior_low=9.90) == (None, 0.0)  # at close
    assert ladder.planned_usd(10.50, prior_close=10.00, prior_low=9.90) == (None, 0.0)  # above close
    assert ladder.planned_usd(9.995, prior_close=10.00, prior_low=9.90) == (None, 0.0)  # 0.05% < 0.1%


def test_ladder_is_stepped_like_dca_ladder_core():
    # Same rule as dca_ladder_core.ladder_multiplier: the largest threshold <= drop wins, raw
    # float compare, no epsilon (mirroring live, not "fixing" it). 9.75 is a float-exact 2.5%
    # drop -> [2.0, 2.75) -> 3.0x -> $6.00. (9.80 is NOT used: 10 - 9.80 = 0.19999..., which
    # live sizing also reads as 1.99999% -> 2.5x.)
    ladder = _mod().load_global_ladder(REPO_CONFIG)
    assert ladder.planned_usd(9.75, prior_close=10.00, prior_low=9.90) == ("below_low", pytest.approx(6.00))


def test_ladder_uses_base_amounts_from_config():
    cfg = json.loads((REPO_CONFIG / "sizing_ladder.json").read_text())
    ladder = _mod().load_global_ladder(REPO_CONFIG)
    assert ladder.base_below_low_usd == cfg["base_amount_usd"]
    assert ladder.base_below_close_usd == cfg["base_amount_below_close_usd"]


def test_all_caps_come_from_budgets_json():
    cfg = json.loads((REPO_CONFIG / "budgets.json").read_text())
    assert _mod().load_daily_caps(REPO_CONFIG) == tuple(t["daily_cap_usd"] for t in cfg["budget_tiers"])


# --- the core count ---------------------------------------------------------------------------------


def test_deep_dip_after_budget_spent_is_one_below_low_event(ledger, staging_dir):
    # Spent $180 at 09:35; remaining 0 < planned $9.00 -> event.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    report = _run(ledger)
    assert report["sessions_with_event"] == 1
    assert report["events"] == [
        {"session": D1, "symbol": "ZZZA", "slot": "13:30", "tier": "below_low", "price": DEEP,
         "prior_close": 10.00, "prior_low": pytest.approx(9.90), "planned_usd": pytest.approx(9.00),
         "spent_before_usd": 180.0, "remaining_usd": 0.0}
    ]
    assert report["by_tier"]["below_low"]["sessions_with_event"] == 1
    assert report["by_tier"]["below_close_only"]["sessions_with_event"] == 0


def test_remaining_just_below_planned_is_exhausted(ledger, staging_dir):
    # Spent $171.50; remaining 8.50 < 9.00.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=171.5)])
    assert _run(ledger)["sessions_with_event"] == 1


def test_remaining_equal_to_planned_is_not_exhausted(ledger, staging_dir):
    # Spent $171; remaining 9.00 is NOT < 9.00 -> the buy was fundable.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=171.0)])
    report = _run(ledger)
    assert report["sessions_with_event"] == 0
    assert report["events"] == []


def test_budget_left_means_no_event(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=100.0)])
    assert _run(ledger)["sessions_with_event"] == 0


def test_shallow_dip_counts_as_below_close_only(ledger, staging_dir):
    # 9.97: planned $1.00; spent $180 -> remaining 0 < 1 -> event, in the below_close_only bucket.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], quote_price=SHALLOW)
    report = _run(ledger)
    assert report["by_tier"]["below_close_only"]["sessions_with_event"] == 1
    assert report["by_tier"]["below_low"]["sessions_with_event"] == 0
    assert [e["tier"] for e in report["events"]] == ["below_close_only"]


def test_shallow_dip_with_a_dollar_left_is_fundable(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=179.0)], quote_price=SHALLOW)
    assert _run(ledger)["sessions_with_event"] == 0


@pytest.mark.parametrize("price", [10.00, 10.50, 9.995])
def test_no_buy_prices_are_never_events(ledger, staging_dir, price):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], quote_price=price)
    assert _run(ledger)["sessions_with_event"] == 0


def test_price_exactly_at_prior_low_is_below_close_only(ledger, staging_dir):
    # Live base rule (dca_ladder_core.select_base_usd) is STRICT: decision_price < previous_low
    # for the $2 base, so a price AT the low takes the $1 base. The drop is 10 - 9.90 = 0.0999...
    # in float, i.e. 0.99999% -> [0.5, 1.0) -> 1.5x, as live sizing reads it -> planned $1.50.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], quote_price=9.90)
    (event,) = _run(ledger)["events"]
    assert event["tier"] == "below_close_only"
    assert event["planned_usd"] == pytest.approx(1.50)


# --- what counts as spent ---------------------------------------------------------------------------


def test_manual_fills_never_count_toward_spent(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("m1", notional=180.0, run_kind="manual")])
    assert _run(ledger)["sessions_with_event"] == 0


def test_fills_after_the_slot_do_not_count(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0, filled_at=FILL_1530, slot="15:30")])
    assert _run(ledger)["sessions_with_event"] == 0


def test_spend_sums_across_tickers(ledger, staging_dir):
    # The daily cap is account-wide: $100 ZZZA + $80 ZZZB = $180 spent before 13:30.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=100.0), _fill("o2", notional=80.0, symbol="ZZZB")])
    assert _run(ledger)["sessions_with_event"] == 1


# --- all four caps ----------------------------------------------------------------------------------


def test_all_caps_runs_once_per_budget_tier(ledger, staging_dir):
    # Spent $180: exhausted only at the $180 cap; at $240 remaining $60 >= $9.00.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    reports = _mod().budget_exhaustion_opportunity_all_caps(ledger, known_at=KNOW_ALL, config_dir=REPO_CONFIG)
    assert [r["daily_cap_usd"] for r in reports] == [180.0, 240.0, 300.0, 360.0]
    assert [r["sessions_with_event"] for r in reports] == [1, 0, 0, 0]


# --- point in time and fail closed ------------------------------------------------------------------


def test_rows_not_yet_known_are_invisible(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    report = _run(ledger, known_at=utc(2026, 4, 1))
    assert report["sessions_with_event"] == 0
    assert report["quotes_considered"] == 0


def test_quote_fetched_after_known_at_is_invisible(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], quote_as_of=LATER)
    assert _run(ledger, known_at=FIRST_SEEN)["quotes_considered"] == 0


def test_missing_prior_bar_is_skipped_and_counted_not_guessed(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], with_prior_bar=False)
    report = _run(ledger)
    assert report["sessions_with_event"] == 0
    assert report["skipped_no_prior_bar"] == 1


@pytest.mark.parametrize("cap", [0.0, -1.0, math.nan, math.inf, True])
def test_bad_daily_cap_raises(ledger, cap):
    with pytest.raises(ValueError, match="daily_cap_usd"):
        _run(ledger, daily_cap_usd=cap)


def test_naive_known_at_is_refused(ledger):
    with pytest.raises(ValueError, match="known_at"):
        _run(ledger, known_at=datetime(2026, 6, 1))


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _mod().load_global_ladder(tmp_path)


# --- read-only and reporting shape ------------------------------------------------------------------


def test_function_never_writes(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    before = {t: ledger.row_count(t) for t in ledger.tables()}
    _run(ledger)
    _mod().budget_exhaustion_opportunity_all_caps(ledger, known_at=KNOW_ALL, config_dir=REPO_CONFIG)
    assert {t: ledger.row_count(t) for t in ledger.tables()} == before


def test_report_echoes_its_parameters(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    report = _run(ledger)
    assert report["daily_cap_usd"] == CAP
    assert report["known_at"] == KNOW_ALL
    assert report["ladder"] == "global"
    assert report["sessions_observed"] == 1
    assert report["quotes_considered"] == 1
