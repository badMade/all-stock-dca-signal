"""Outcomes: price at fixed session horizons, ASOF on quote_date, never a later quote.

Fixture (all synthetic, see conftest): fill of ZZZA on D0 = SESSIONS[0] = 2026-03-02 at
$10.00, $100 notional. Horizons are the proposed defaults t+5 and t+20 sessions, so the
horizon sessions are SESSIONS[5] = 2026-03-09 and SESSIONS[20] = 2026-03-30.

Bars use deliberately distinct closes so a leak is obvious:
  SESSIONS[4] close 10.50, SESSIONS[5] close 11.00, SESSIONS[6] close 50.00 (a spike
  one session AFTER t+5 -- if t+5 ever reports 50, lookahead happened),
  SESSIONS[19] close 12.00, SESSIONS[20] close 13.00, SESSIONS[21] close 99.00.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from .conftest import (
    FIRST_SEEN,
    LATER,
    SESSIONS,
    bar,
    baseline,
    fill,
    forward_split,
    ingest_bars,
    ingest_calendar,
    utc,
)

KNOW_ALL = utc(2026, 6, 1)
CLOSES = {4: 10.50, 5: 11.00, 6: 50.00, 19: 12.00, 20: 13.00, 21: 99.00}


def _bars(skip=(), symbol="ZZZA"):
    return [bar(symbol, SESSIONS[i], c) for i, c in CLOSES.items() if i not in skip]


def _score(ledger, known_at=KNOW_ALL, run_id="score-1", as_of=LATER):
    return ledger.record_outcomes(known_at=known_at, run_id=run_id, as_of=as_of)


def _by_horizon(ledger, subject_id, known_at=KNOW_ALL):
    rows = [r for r in ledger.outcomes(known_at=known_at) if r["subject_id"] == subject_id]
    return {r["horizon_sessions"]: r for r in rows}


def test_default_horizons_are_t5_and_t20():
    from market_data_ingest.ledger import defaults

    assert defaults.OUTCOME_HORIZONS_SESSIONS == (5, 20)


def test_outcome_uses_the_close_on_the_horizon_session(ledger, staging_dir):
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars())
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    assert _score(ledger) == 2  # one row per horizon

    out = _by_horizon(ledger, "ord-1")
    # t+5: 11.00 / 10.00 - 1 = +10%.
    assert out[5]["horizon_session"] == SESSIONS[5]
    assert out[5]["quote_date"] == SESSIONS[5]
    assert out[5]["status"] == "scored"
    assert out[5]["return_pct"] == pytest.approx(0.10)
    # t+20: 13.00 / 10.00 - 1 = +30%.
    assert out[20]["quote_date"] == SESSIONS[20]
    assert out[20]["return_pct"] == pytest.approx(0.30)


def test_outcome_never_uses_a_quote_dated_after_the_horizon_session(ledger, staging_dir):
    # The t+5 bar is MISSING. ASOF semantics must fall back to the latest quote ON OR
    # BEFORE the horizon session (SESSIONS[4], 10.50 -> +5%), never forward to the
    # SESSIONS[6] spike at 50.00 (which would read +400%).
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars(skip={5, 20}))
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger)

    out = _by_horizon(ledger, "ord-1")
    for h, row in out.items():
        assert row["quote_date"] <= row["horizon_session"], f"t+{h} read a quote after its horizon"
    assert out[5]["quote_date"] == SESSIONS[4]
    assert out[5]["return_pct"] == pytest.approx(0.05)  # 10.50 / 10.00 - 1
    # t+20 likewise falls back to SESSIONS[19] (12.00 -> +20%), not SESSIONS[21] (99.00).
    assert out[20]["quote_date"] == SESSIONS[19]
    assert out[20]["return_pct"] == pytest.approx(0.20)


def test_a_quote_ingested_after_known_at_is_invisible(ledger, staging_dir):
    # The t+5 bar exists but was only learned at LATER. Scored as of a known_at before
    # LATER, the outcome must use SESSIONS[4] (10.50, +5%) -- the bar did not exist yet.
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars(skip={5}))
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[5], 11.00)], as_of=LATER, run_id="bars-late")
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)

    before = FIRST_SEEN + timedelta(hours=1)
    _score(ledger, known_at=before, run_id="score-early", as_of=before)
    early = _by_horizon(ledger, "ord-1", known_at=before)
    assert early[5]["quote_date"] == SESSIONS[4]
    assert early[5]["return_pct"] == pytest.approx(0.05)

    _score(ledger, known_at=KNOW_ALL, run_id="score-late", as_of=KNOW_ALL)
    late = _by_horizon(ledger, "ord-1", known_at=KNOW_ALL)
    assert late[5]["quote_date"] == SESSIONS[5]
    assert late[5]["return_pct"] == pytest.approx(0.10)
    # The early score is still in the table, untouched (append-only history of scores).
    assert _by_horizon(ledger, "ord-1", known_at=before)[5]["return_pct"] == pytest.approx(0.05)


def test_horizon_beyond_the_calendar_is_unscored_not_zero(ledger, staging_dir):
    # Calendar only through SESSIONS[10]: t+5 is reachable, t+20 is not yet.
    ingest_calendar(ledger, sessions=SESSIONS[:11])
    ingest_bars(ledger, staging_dir, _bars())
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger)

    out = _by_horizon(ledger, "ord-1")
    assert out[5]["status"] == "scored"
    assert out[20]["status"] == "unscored"
    assert out[20]["unscored_reason"] == "horizon_not_reached"
    assert out[20]["return_pct"] is None  # never 0.0

    # Once the calendar catches up, a re-score appends a scored row; readers see it.
    ingest_calendar(ledger, run_id="cal-2", as_of=LATER)
    _score(ledger, run_id="score-2", as_of=KNOW_ALL)
    assert _by_horizon(ledger, "ord-1")[20]["status"] == "scored"


def test_no_quote_after_the_fill_session_is_unscored(ledger, staging_dir):
    # Only a bar ON the fill session exists. An "outcome" equal to the entry day is not an
    # outcome, so both horizons are unscored with reason no_quote.
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[0], 10.00)])
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger)
    out = _by_horizon(ledger, "ord-1")
    assert {r["status"] for r in out.values()} == {"unscored"}
    assert {r["unscored_reason"] for r in out.values()} == {"no_quote"}


def test_rescoring_identical_inputs_is_a_no_op(ledger, staging_dir):
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars())
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    assert _score(ledger, run_id="s1") == 2
    assert _score(ledger, run_id="s2") == 0
    assert ledger.row_count("outcomes") == 2


def test_split_between_fill_and_horizon_is_applied_to_the_horizon_close(ledger, staging_dir):
    # 2-for-1 forward split ex SESSIONS[3]. Raw t+5 close is 5.50 post-split; in the fill's
    # pre-split share basis that is 5.50 / (1/2) = 11.00, so the return is +10%, not -45%.
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[5], 5.50), bar("ZZZA", SESSIONS[20], 6.50)])
    ledger.prices.ingest_corporate_actions(
        [forward_split("ZZZA", SESSIONS[3], 1, 2)], run_id="ca", as_of=FIRST_SEEN, staging_dir=staging_dir
    )
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger)
    out = _by_horizon(ledger, "ord-1")
    assert out[5]["close_raw"] == pytest.approx(5.50)
    assert out[5]["price_factor"] == pytest.approx(0.5)
    assert out[5]["return_pct"] == pytest.approx(0.10)
    assert out[20]["return_pct"] == pytest.approx(0.30)  # 6.50 / 0.5 = 13.00 vs 10.00


def test_baseline_is_a_flat_1x_buy_at_the_same_slot_and_price(ledger, staging_dir):
    from market_data_ingest.ledger import defaults

    assert defaults.BASELINE_MULTIPLIER == 1.0
    assert defaults.BASELINE_RULE == "flat_1x_same_slot_price"

    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars())
    # The graded ladder bought 2x ($100) at 10.00; the baseline buys 1x of the $50 base.
    ledger.ingest_fills([fill("ord-1", notional=100.0, multiplier=2.0)], source="run_artifact", run_id="f1",
                        as_of=FIRST_SEEN)
    ledger.ingest_baseline_fills([baseline("dec-1", base_notional=50.0)], source="run_artifact", run_id="b1",
                                 as_of=FIRST_SEEN)
    stored = ledger.baseline_fills(known_at=KNOW_ALL)
    assert len(stored) == 1
    assert stored[0]["notional"] == pytest.approx(50.0)  # 1.0 x 50
    assert stored[0]["quantity"] == pytest.approx(5.0)  # 50 / 10.00
    assert stored[0]["baseline_rule"] == "flat_1x_same_slot_price"

    _score(ledger)
    base = _by_horizon(ledger, "dec-1")
    assert base[5]["subject_kind"] == "baseline"
    assert base[5]["return_pct"] == pytest.approx(0.10)  # same price path as the fill
