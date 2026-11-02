"""Universe tables (research spec "Dynamic 100-ticker universe", 2026-11-01): append-only
scores, proposals, and dated membership with a point-in-time "members as of D" query.

Nothing here selects a universe or edits config/tickers.json; these are the tables a
future scorer writes to and a backtest reads from. Tickers are fake (ZZZA ...).

Membership intervals are half-open: a ticker is a member on D when
``effective_from <= D < effective_to`` (``effective_to`` NULL = still a member). Ending a
membership APPENDS a new version of the same (ticker, effective_from) row with
``effective_to`` set and a later ``as_of``; the open version is never rewritten.
"""

from __future__ import annotations

from datetime import date

import pytest

from .conftest import FIRST_SEEN, LATER, utc

KNOW_ALL = utc(2026, 6, 1)
Q1, Q2 = date(2026, 1, 2), date(2026, 4, 1)


def score(ticker, as_of_date=Q2, **kw):
    row = {
        "as_of_date": as_of_date,
        "ticker": ticker,
        "sector_sic": "3674",
        "momentum": 0.5,
        "profitability": 0.2,
        "low_vol": -0.1,
        "value": 0.0,
        "composite": 0.15,  # (0.5 + 0.2 - 0.1 + 0.0) / 4
        "rank": 1,
        "source_quote_date": date(2026, 3, 31),
        "source_filed_date": date(2026, 2, 20),
    }
    row.update(kw)
    return row


def member(ticker, effective_from, effective_to=None, approval_id="appr-1"):
    return {"ticker": ticker, "effective_from": effective_from, "effective_to": effective_to,
            "approval_id": approval_id}


# --- universe_scores -----------------------------------------------------------------------


def test_scores_round_trip_and_reingest_is_a_no_op(ledger):
    rows = [score("ZZZA"), score("ZZZB", rank=2, composite=None, value=None, profitability=None)]
    assert ledger.ingest_universe_scores(rows, source="scorer", run_id="s1", as_of=FIRST_SEEN) == 2
    assert ledger.ingest_universe_scores(rows, source="scorer", run_id="s2", as_of=LATER) == 0
    got = {r["ticker"]: r for r in ledger.universe_scores(as_of_date=Q2, known_at=KNOW_ALL)}
    assert got["ZZZA"]["composite"] == pytest.approx(0.15)
    assert got["ZZZB"]["composite"] is None  # unscored (>= 2 factors missing), never 0


@pytest.mark.parametrize(
    "field,value",
    [("source_quote_date", date(2026, 4, 2)), ("source_filed_date", date(2026, 4, 2))],
)
def test_scores_refuse_inputs_dated_after_the_score_date(ledger, field, value):
    # Lookahead guard: a score as of 2026-04-01 cannot rest on a quote or filing from 04-02.
    from market_data_ingest.ledger.errors import LedgerValidationError

    with pytest.raises(LedgerValidationError, match=field):
        ledger.ingest_universe_scores([score("ZZZA", **{field: value})], source="scorer", run_id="s1",
                                      as_of=FIRST_SEEN)


# --- universe_proposals --------------------------------------------------------------------


def test_proposals_round_trip(ledger):
    proposal = {
        "review_id": "2026Q2",
        "as_of_date": Q2,
        "adds": ["ZZZC", "ZZZD"],
        "drops": ["ZZZB"],
        "reasons": {"ZZZC": "rank 12 at this and prior review", "ZZZB": "hard fail: delisted"},
        "safeguard_results": {"pool_coverage": "pass", "max_swaps": "pass"},
        "status": "proposed",
    }
    assert ledger.ingest_universe_proposals([proposal], source="scorer", run_id="p1", as_of=FIRST_SEEN) == 1
    (got,) = ledger.universe_proposals(known_at=KNOW_ALL)
    assert got["adds"] == ["ZZZC", "ZZZD"]
    assert got["drops"] == ["ZZZB"]
    assert got["reasons"]["ZZZB"] == "hard fail: delisted"
    assert got["safeguard_results"] == {"pool_coverage": "pass", "max_swaps": "pass"}


def test_proposal_status_vocabulary_is_closed(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError

    bad = {"review_id": "r", "as_of_date": Q2, "adds": [], "drops": [], "reasons": {}, "safeguard_results": {},
           "status": "approved"}  # approval is the operator's act, never a scorer row
    with pytest.raises(LedgerValidationError, match="status"):
        ledger.ingest_universe_proposals([bad], source="scorer", run_id="p1", as_of=FIRST_SEEN)


# --- universe_membership -------------------------------------------------------------------


def _membership(ledger):
    # ZZZA: member from Q1, still open.
    # ZZZB: member from Q1; first recorded open (FIRST_SEEN), exit to Q2 appended at LATER.
    # ZZZC: member from Q2 (the swap-in for ZZZB).
    ledger.ingest_universe_membership([member("ZZZA", Q1), member("ZZZB", Q1)], source="operator_approval",
                                      run_id="m1", as_of=FIRST_SEEN)
    ledger.ingest_universe_membership([member("ZZZB", Q1, Q2, approval_id="appr-2"),
                                       member("ZZZC", Q2, approval_id="appr-2")],
                                      source="operator_approval", run_id="m2", as_of=LATER)


def test_members_as_of_date(ledger):
    _membership(ledger)
    assert ledger.members_as_of(date(2025, 12, 31), known_at=KNOW_ALL) == []
    assert ledger.members_as_of(Q1, known_at=KNOW_ALL) == ["ZZZA", "ZZZB"]
    assert ledger.members_as_of(date(2026, 3, 31), known_at=KNOW_ALL) == ["ZZZA", "ZZZB"]
    # effective_to is exclusive: on Q2 ZZZB is out and ZZZC is in.
    assert ledger.members_as_of(Q2, known_at=KNOW_ALL) == ["ZZZA", "ZZZC"]


def test_members_as_of_respects_known_at(ledger):
    _membership(ledger)
    # Before the exit was recorded (between FIRST_SEEN and LATER), ZZZB still looked open
    # and ZZZC was unknown -- the answer a backtest run at that moment would have seen.
    between = utc(2026, 4, 30, 12)
    assert ledger.members_as_of(date(2026, 5, 1), known_at=between) == ["ZZZA", "ZZZB"]
    assert ledger.members_as_of(date(2026, 5, 1), known_at=KNOW_ALL) == ["ZZZA", "ZZZC"]
    # Both versions of ZZZB are still stored: the open one was not overwritten.
    assert ledger.row_count("universe_membership") == 4


def test_membership_macro_is_usable_from_sql(ledger):
    _membership(ledger)
    rows = ledger.con.execute(
        "SELECT ticker FROM universe_members_as_of(DATE '2026-04-01', TIMESTAMP '9999-12-31') ORDER BY ticker"
    ).fetchall()
    assert [r[0] for r in rows] == ["ZZZA", "ZZZC"]


def test_membership_interval_must_be_non_empty(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError

    with pytest.raises(LedgerValidationError, match="effective_to"):
        ledger.ingest_universe_membership([member("ZZZA", Q2, Q1)], source="operator_approval", run_id="m1",
                                          as_of=FIRST_SEEN)
    with pytest.raises(LedgerValidationError, match="approval_id"):
        ledger.ingest_universe_membership([member("ZZZA", Q1, approval_id="")], source="operator_approval",
                                          run_id="m1", as_of=FIRST_SEEN)
