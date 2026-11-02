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


def _score(ledger, known_at=KNOW_ALL, run_id="score-1", as_of=KNOW_ALL, **kw):
    # as_of defaults to known_at: a score cannot be written before the data it used was known.
    return ledger.record_outcomes(known_at=known_at, run_id=run_id, as_of=as_of, **kw)


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
    # The t+5 and t+20 bars are MISSING. The quote must never come from AFTER the horizon
    # (SESSIONS[6] at 50.00 would read +400%). With the default tolerance of 0 sessions the
    # earlier SESSIONS[4] close is stale too, so both horizons are unscored (review of #2101).
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars(skip={5, 20}))
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger)

    out = _by_horizon(ledger, "ord-1")
    for h, row in out.items():
        assert row["status"] == "unscored", f"t+{h} scored a missing bar"
        assert row["unscored_reason"] == "stale_quote"
        assert row["return_pct"] is None
        assert row["quote_date"] is None


def test_default_quote_tolerance_is_zero_sessions():
    from market_data_ingest.ledger import defaults

    assert defaults.OUTCOME_QUOTE_TOLERANCE_SESSIONS == 0


def test_an_explicit_tolerance_allows_an_asof_fallback_but_never_forward(ledger, staging_dir):
    # With tolerance 1, t+5 may fall back ONE session to SESSIONS[4] (10.50 -> +5%) and t+20
    # to SESSIONS[19] (12.00 -> +20%), still never forward to SESSIONS[6] / SESSIONS[21].
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars(skip={5, 20}))
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger, quote_tolerance_sessions=1)

    out = _by_horizon(ledger, "ord-1")
    for h, row in out.items():
        assert row["quote_date"] <= row["horizon_session"], f"t+{h} read a quote after its horizon"
        assert row["quote_tolerance_sessions"] == 1
    assert out[5]["quote_date"] == SESSIONS[4]
    assert out[5]["return_pct"] == pytest.approx(0.05)  # 10.50 / 10.00 - 1
    assert out[20]["quote_date"] == SESSIONS[19]
    assert out[20]["return_pct"] == pytest.approx(0.20)


def test_a_quote_many_sessions_old_is_stale_not_scored(ledger, staging_dir):
    # Reviewer's probe on #2101: only a t+1 bar exists (12.00). It must not be reported as the
    # t+5 or t+20 outcome (+20%), and a tolerance of 1 is still far too small for it.
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[1], 12.00)])
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger, quote_tolerance_sessions=1)
    out = _by_horizon(ledger, "ord-1")
    assert {r["status"] for r in out.values()} == {"unscored"}
    assert {r["unscored_reason"] for r in out.values()} == {"stale_quote"}


def test_a_quote_ingested_after_known_at_is_invisible(ledger, staging_dir):
    # The t+5 bar exists but was only learned at LATER. Scored as of a known_at before LATER
    # it did not exist yet, so t+5 is unscored (stale); scored later it is +10%.
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars(skip={5}))
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[5], 11.00)], as_of=LATER, run_id="bars-late")
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)

    before = FIRST_SEEN + timedelta(hours=1)
    _score(ledger, known_at=before, run_id="score-early", as_of=before)
    early = _by_horizon(ledger, "ord-1", known_at=before)
    assert early[5]["status"] == "unscored"
    assert early[5]["unscored_reason"] == "stale_quote"

    _score(ledger, known_at=KNOW_ALL, run_id="score-late", as_of=KNOW_ALL)
    late = _by_horizon(ledger, "ord-1", known_at=KNOW_ALL)
    assert late[5]["quote_date"] == SESSIONS[5]
    assert late[5]["return_pct"] == pytest.approx(0.10)
    # The early score is still in the table, untouched (append-only history of scores).
    assert _by_horizon(ledger, "ord-1", known_at=before)[5]["status"] == "unscored"


def test_score_cannot_be_stamped_before_the_data_it_used(ledger, staging_dir):
    # Reviewer's probe on #2101: known_at=2026-05-01 with as_of=2026-03-03 would let
    # outcomes(known_at=2026-03-04) return a row scored with May data. Refused, nothing written.
    from market_data_ingest.ledger.errors import LedgerValidationError

    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars())
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    with pytest.raises(LedgerValidationError, match="as_of"):
        ledger.record_outcomes(known_at=utc(2026, 5, 1), run_id="leak", as_of=utc(2026, 3, 3))
    assert ledger.row_count("outcomes") == 0


def test_outcomes_reader_filters_on_the_rows_known_at(ledger, staging_dir):
    # Defence in depth: a row scored with known_at=KNOW_ALL is invisible to a reader asking
    # "as known at" any earlier moment, whatever its as_of says.
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars())
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger, known_at=KNOW_ALL, as_of=KNOW_ALL)
    assert ledger.outcomes(known_at=KNOW_ALL - timedelta(seconds=1)) == []
    assert len(ledger.outcomes(known_at=KNOW_ALL)) == 2


def test_a_later_rescore_with_older_information_does_not_win(ledger, staging_dir):
    # Score with full information (known_at=KNOW_ALL), then re-score LATER (as_of further
    # out) with an EARLIER known_at, before the t+5 bar was known. The reader must prefer the
    # row built from the newest information (known_at DESC), not the newest write.
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, _bars(skip={5}))
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[5], 11.00)], as_of=LATER, run_id="bars-late")
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger, known_at=KNOW_ALL, run_id="full", as_of=KNOW_ALL)
    early_info = FIRST_SEEN + timedelta(hours=1)
    _score(ledger, known_at=early_info, run_id="stale-rescore", as_of=KNOW_ALL + timedelta(days=1))
    out = _by_horizon(ledger, "ord-1", known_at=KNOW_ALL + timedelta(days=2))
    assert out[5]["status"] == "scored"
    assert out[5]["return_pct"] == pytest.approx(0.10)


def test_fill_on_a_session_missing_from_the_calendar_is_unscored(ledger, staging_dir):
    # If the fill session is absent from market_sessions, counting sessions <= it would shift
    # every horizon one session early. Refuse to guess: unscored, session_not_in_calendar.
    ingest_calendar(ledger, sessions=[d for d in SESSIONS if d != SESSIONS[0]])
    ingest_bars(ledger, staging_dir, _bars())
    ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    _score(ledger)
    out = _by_horizon(ledger, "ord-1")
    assert {r["status"] for r in out.values()} == {"unscored"}
    assert {r["unscored_reason"] for r in out.values()} == {"session_not_in_calendar"}


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

    # Once the calendar catches up (learned after the first score), a re-score with the newer
    # known_at appends a scored row and readers prefer it. (Before the #2101 review fix this
    # backdated the calendar row to LATER and re-scored at the SAME known_at, a tie that only
    # resolved by write order.)
    caught_up = KNOW_ALL + timedelta(days=1)
    ingest_calendar(ledger, run_id="cal-2", as_of=caught_up)
    _score(ledger, known_at=caught_up, run_id="score-2", as_of=caught_up)
    assert _by_horizon(ledger, "ord-1", known_at=caught_up)[20]["status"] == "scored"


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
