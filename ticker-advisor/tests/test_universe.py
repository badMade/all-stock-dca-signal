"""Tests for universe.py -- the research-only dynamic 100-ticker universe engine.

Pure stdlib, like test_scoring.py: nothing here needs the heavy signal stack.

Every input below is SYNTHETIC and hand-built. No figure is real market data
and none is claimed to be: tickers are generated placeholders (``SAA``,
``SAB``, ...) or obviously made-up symbols (``AAA``/``BBB``), and composites
are chosen only so that each ticker's rank is trivially computable by hand.
The expected output of every test is derived in its comments from the rules
in the design spec (Nasdaq-100-style 75/150 hysteresis, two-consecutive-
review entry, 25/sector + 10/industry caps, 100 max, 2-quarter tenure,
8-swap cap, hard-fail exits, bench 101-250, fail-closed safeguards).
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime, timezone

import pytest

from universe import (
    Candidate,
    FactorInputs,
    Incumbent,
    InputFreshness,
    PoolStats,
    PoolThresholds,
    PriorRunStats,
    Proposal,
    ScoredTicker,
    TickerStatus,
    UniverseConfig,
    apply_safeguards,
    composite_scores,
    compute_pool_stats,
    detect_hard_fails,
    filter_pool,
    ineligible_tickers,
    proposal_to_dict,
    rank_tickers,
    sector_zscores,
    select_universe,
    validate_proposal,
    winsorize,
    write_proposal,
)

AS_OF = date(2026, 10, 30)
GENERATED_AT = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
# sqrt(3/2): the population z-score of the top/bottom value in any evenly
# spaced 3-point sector (values 1,2,3 -> mean 2, pstdev sqrt(2/3)).
K3 = math.sqrt(1.5)


# --------------------------------------------------------------------------
# Synthetic-board helpers
# --------------------------------------------------------------------------


def _sym(i: int) -> str:
    """Placeholder symbol #i: ``SAA``, ``SAB``, ... -- lexical order == i order.

    Letters only so every symbol satisfies scoring.TICKER_SYMBOL_PATTERN, and
    lexical order matching index order means a tie broken "by ticker symbol"
    is broken in the same direction as the index, which keeps hand-derived
    expectations easy to read.
    """
    return "S" + chr(65 + i // 26) + chr(65 + i % 26)


def _rank_sym(rank: int) -> str:
    """The synthetic symbol that _board() places at 1-based ``rank``."""
    return _sym(rank - 1)


def _board(n: int = 200, *, sectors: int = 10, industries: int = 20, drop=()) -> dict[str, ScoredTicker]:
    """n scored tickers where _sym(i) has composite n-i, i.e. rank i+1.

    Sector = i % 10 and industry = i % 20, so industry k always belongs to
    sector k % 10 (a consistent hierarchy) and the top 100 hold exactly 10
    per sector and 5 per industry -- well under the 25/10 default caps, so
    caps never interfere with the hysteresis tests that use this board.
    """
    board = {}
    for i in range(n):
        ticker = _sym(i)
        if ticker in drop:
            continue
        board[ticker] = ScoredTicker(
            ticker=ticker,
            sector=f"SEC{i % sectors}",
            industry=f"IND{i % industries}",
            composite=float(n - i),
        )
    return board


def _prior_same(board) -> dict[str, int]:
    """Prior-review ranks identical to this review's ranks."""
    return rank_tickers(board)


def _incumbents(ranks, tenure: int = 4) -> dict[str, Incumbent]:
    return {_rank_sym(r): Incumbent(tenure_quarters=tenure) for r in ranks}


def _select(board, prior, incumbents, hard_fails=None, config=None, pool_stats=None, ineligible=None):
    return select_universe(
        board,
        prior,
        incumbents,
        hard_fails or {},
        config or UniverseConfig(),
        review_id="2026Q4-test",
        as_of_date=AS_OF,
        generated_at=GENERATED_AT,
        pool_stats=pool_stats or PoolStats(pool_size=1000, factor_missing_fraction=_ZERO_MISSING),
        ineligible=ineligible,
    )


_ZERO_MISSING = {"momentum": 0.0, "profitability": 0.0, "low_volatility": 0.0, "value": 0.0}


def _fresh() -> InputFreshness:
    return InputFreshness(quote_date=date(2026, 10, 30), quote_age_sessions=0, edgar_refreshed_date=date(2026, 10, 29))


def _prior_stats(pool_size: int = 1000) -> PriorRunStats:
    return PriorRunStats(pool_size=pool_size)


def _codes(failures) -> set[str]:
    return {failure.code for failure in failures}


def _clean_proposal(**pool_overrides) -> Proposal:
    """No-change review: 100 incumbents at ranks 1..100, nothing to add or drop."""
    board = _board(200)
    pool = PoolStats(
        pool_size=pool_overrides.get("pool_size", 1000),
        factor_missing_fraction=pool_overrides.get("factor_missing_fraction", _ZERO_MISSING),
    )
    return _select(board, _prior_same(board), _incumbents(range(1, 101)), pool_stats=pool)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_default_config_matches_spec_values():
    # Every spec threshold is a named, overridable field whose default is
    # the spec's (not-yet-operator-approved) proposed value.
    cfg = UniverseConfig()
    assert cfg.max_active == 100 and cfg.min_active == 90
    assert cfg.entry_rank == 75 and cfg.incumbent_keep_rank == 150
    assert cfg.sector_cap == 25 and cfg.industry_cap == 10
    assert cfg.min_tenure_quarters == 2 and cfg.max_swaps == 8
    assert (cfg.bench_min_rank, cfg.bench_max_rank) == (101, 250)
    assert cfg.min_pool_coverage == 0.95
    assert cfg.max_quote_age_sessions == 3 and cfg.max_edgar_age_days == 7
    assert cfg.max_quote_age_calendar_days == 7
    assert cfg.max_factor_missing_fraction == 0.10
    assert cfg.winsor_limit == 3.0 and cfg.max_missing_factors == 1
    assert dict(cfg.factor_weights) == {"momentum": 0.25, "profitability": 0.25, "low_volatility": 0.25, "value": 0.25}
    t = PoolThresholds()
    assert t.min_price == 5.0 and t.min_market_cap == 2e9
    assert t.min_median_dollar_volume_60d == 20e6 and t.min_history_sessions == 252


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_active": 101},  # floor above the ceiling can never be satisfied
        {"entry_rank": 151},  # entry band wider than the keep band inverts the hysteresis
        {"max_swaps": True},  # bool is an int subclass -- must not read as 1
        {"sector_cap": 0},
        {"max_factor_missing_fraction": float("nan")},
        {"factor_weights": {"momentum": 1.0}},  # unknown/missing factor keys
    ],
)
def test_invalid_config_is_refused(overrides):
    with pytest.raises(ValueError):
        UniverseConfig(**overrides)


# --------------------------------------------------------------------------
# Pool filter
# --------------------------------------------------------------------------


def _cand(ticker="AAA", **kw) -> Candidate:
    base = {
        "nms_listed": True,
        "security_type": "common_stock",
        "price": 50.0,
        "market_cap": 10e9,
        "median_dollar_volume_60d": 100e6,
        "history_sessions": 500,
        "fractional_eligible": True,
    }
    base.update(kw)
    return Candidate(ticker=ticker, **base)


def test_filter_pool_keeps_a_candidate_that_clears_every_threshold():
    result = filter_pool([_cand("AAA")], PoolThresholds())
    assert [c.ticker for c in result.eligible] == ["AAA"]
    assert result.rejections == {}


def test_filter_pool_thresholds_are_strict_where_the_spec_says_greater_than():
    # Spec: price > $5, cap > $2B, 60d median $vol > $20M (strict), and
    # >= 252 sessions of history (inclusive). Exactly-at-threshold values
    # therefore fail the first three and pass the last.
    result = filter_pool(
        [
            _cand("PPP", price=5.0),
            _cand("MMM", market_cap=2e9),
            _cand("VVV", median_dollar_volume_60d=20e6),
            _cand("HHH", history_sessions=252),
            _cand("HHG", history_sessions=251),
        ],
        PoolThresholds(),
    )
    assert [c.ticker for c in result.eligible] == ["HHH"]
    assert result.rejections["PPP"] == ("price_at_or_below_min",)
    assert result.rejections["MMM"] == ("market_cap_at_or_below_min",)
    assert result.rejections["VVV"] == ("dollar_volume_at_or_below_min",)
    assert result.rejections["HHG"] == ("history_too_short",)


def test_filter_pool_records_every_reason_and_rejects_missing_or_malformed_values():
    result = filter_pool(
        [
            _cand("XXX", nms_listed=False, security_type="etf", fractional_eligible=False),
            _cand("NNN", price=None),
            _cand("BBB", price=True),  # bool must not pass as the number 1
            _cand("FFF", market_cap=float("nan")),
            _cand("UUU", fractional_eligible=None),  # unknown eligibility is not eligibility
            _cand("LLL", nms_listed=None, security_type=None),
            _cand("brk-b"),
        ],
        PoolThresholds(),
    )
    assert result.eligible == ()
    assert result.rejections["XXX"] == ("not_nms_listed", "not_common_stock", "not_fractional_eligible")
    assert result.rejections["NNN"] == ("missing:price",)
    assert result.rejections["BBB"] == ("missing:price",)
    assert result.rejections["FFF"] == ("missing:market_cap",)
    # Unknown flags are reported as MISSING (not as a failed check), so a
    # caller can tell "data did not arrive" (incumbent holds) apart from
    # "genuinely fails the floor" (incumbent leaves the pool).
    assert result.rejections["UUU"] == ("missing:fractional_eligible",)
    assert result.rejections["LLL"] == ("missing:nms_listed", "missing:security_type")
    assert result.rejections["brk-b"] == ("invalid_symbol",)


def test_filter_pool_rejects_duplicate_symbols_and_orders_eligible_by_ticker():
    result = filter_pool([_cand("CCC"), _cand("AAA"), _cand("DDD"), _cand("DDD")], PoolThresholds())
    assert [c.ticker for c in result.eligible] == ["AAA", "CCC"]
    assert result.rejections["DDD"] == ("duplicate_ticker",)


# --------------------------------------------------------------------------
# Hard fails
# --------------------------------------------------------------------------


def test_detect_hard_fails():
    cfg = UniverseConfig()
    statuses = [
        TickerStatus("AAA", delisted=True),
        TickerStatus("BBB", fractional_eligible=False),
        TickerStatus("CCC", sessions_below_min_price=20),  # spec: price < $5 for 20 sessions
        TickerStatus("DDD", sessions_below_min_price=19),  # one short -> not yet
        TickerStatus("EEE", pending_acquisition=True),
        TickerStatus("FFF", fractional_eligible=None),  # unknown -> missing data -> hold, not exit
        TickerStatus("GGG", delisted=True, pending_acquisition=True),
    ]
    assert detect_hard_fails(statuses, cfg) == {
        "AAA": "delisted",
        "BBB": "fractional_ineligible",
        "CCC": "price_below_min_sustained",
        "EEE": "pending_acquisition",
        "GGG": "delisted,pending_acquisition",
    }


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_sector_zscores_are_within_sector_population_zscores():
    # Sector X: 1,2,3 -> mean 2, pstdev sqrt(2/3) -> z = -sqrt(1.5), 0, +sqrt(1.5).
    # Sector Y: 10,10 -> zero dispersion -> both genuinely AT the mean -> 0.0.
    # Sector Z: one valued name -> no spread to normalize against -> None.
    # QQQ: value missing -> None, and excluded from sector X's statistics.
    values = {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0, "QQQ": None, "DDD": 10.0, "EEE": 10.0, "FFF": 7.0}
    sectors = {"AAA": "X", "BBB": "X", "CCC": "X", "QQQ": "X", "DDD": "Y", "EEE": "Y", "FFF": "Z"}
    z = sector_zscores(values, sectors)
    assert z["AAA"] == pytest.approx(-K3)
    assert z["BBB"] == pytest.approx(0.0)
    assert z["CCC"] == pytest.approx(K3)
    assert z["QQQ"] is None
    assert z["DDD"] == 0.0 and z["EEE"] == 0.0
    assert z["FFF"] is None


def test_sector_zscores_treat_non_finite_and_bool_as_missing():
    values = {"AAA": 1.0, "BBB": float("inf"), "CCC": True, "DDD": 3.0}
    z = sector_zscores(values, dict.fromkeys(values, "X"))
    # Only AAA/DDD are usable: 1,3 -> mean 2, pstdev 1 -> -1, +1.
    assert z == {"AAA": pytest.approx(-1.0), "BBB": None, "CCC": None, "DDD": pytest.approx(1.0)}


def test_winsorize_clips_at_the_limit_and_passes_none_through():
    assert winsorize({"A": 4.2, "B": -5.0, "C": 1.5, "D": None}, 3.0) == {"A": 3.0, "B": -3.0, "C": 1.5, "D": None}


def test_extreme_outlier_is_winsorized_to_three():
    # Ten 0.0s and one 10.0 in one sector. mean = 10/11; the outlier's
    # population z is sqrt(n-1) = sqrt(10) ~= 3.162 > 3, so it clips to 3.0;
    # every other name sits at -1/sqrt(10) ~= -0.316 (unclipped).
    names = [_sym(i) for i in range(11)]
    raw = dict.fromkeys(names, 0.0)
    raw[names[10]] = 10.0
    z = sector_zscores(raw, dict.fromkeys(names, "X"))
    assert z[names[10]] == pytest.approx(math.sqrt(10))
    clipped = winsorize(z, 3.0)
    assert clipped[names[10]] == 3.0
    assert clipped[names[0]] == pytest.approx(-1 / math.sqrt(10))


def _fi(ticker, sector="X", industry="X1", m6=None, m12=None, prof=None, vol=None, value=None) -> FactorInputs:
    return FactorInputs(
        ticker=ticker,
        sector=sector,
        industry=industry,
        momentum_6m=m6,
        momentum_12m=m12,
        profitability=prof,
        volatility_1y=vol,
        value=value,
    )


def test_composite_scores_four_factor_equal_weight_with_one_missing_factor():
    # One 3-name sector X. Per factor (z within X, all |z| < 3 so no clipping):
    #   momentum:  m6 = m12 = 1,2,3      -> AAA -K3, BBB 0, CCC +K3 (avg of two identical z's)
    #   profit:    3,2,1                 -> AAA +K3, BBB 0, CCC -K3
    #   low vol:   vol 1,2,3, NEGATED     -> AAA +K3, BBB 0, CCC -K3 (lowest vol scores best)
    #   value:     AAA missing, BBB 2, CCC 3 -> z over {2,3}: BBB -1, CCC +1
    # Composites (mean of available factors -- spec: missing factor ->
    # average of the factors that ARE available):
    #   AAA: (-K3 + K3 + K3) / 3          = K3/3
    #   BBB: (0 + 0 + 0 - 1) / 4           = -0.25
    #   CCC: (K3 - K3 - K3 + 1) / 4        = (1 - K3)/4
    inputs = [
        _fi("AAA", m6=1, m12=1, prof=3, vol=1, value=None),
        _fi("BBB", m6=2, m12=2, prof=2, vol=2, value=2),
        _fi("CCC", m6=3, m12=3, prof=1, vol=3, value=3),
    ]
    scored = composite_scores(inputs, UniverseConfig())
    assert scored["AAA"].composite == pytest.approx(K3 / 3)
    assert scored["BBB"].composite == pytest.approx(-0.25)
    assert scored["CCC"].composite == pytest.approx((1 - K3) / 4)
    assert scored["AAA"].missing_factors == ("value",)
    assert scored["AAA"].factors["low_volatility"] == pytest.approx(K3)
    assert scored["BBB"].sector == "X" and scored["BBB"].industry == "X1"


def test_momentum_uses_whichever_horizon_is_available():
    # m12 missing for AAA only: momentum = AAA's z(m6) alone.
    # m6 over X = 1,2,3 -> AAA -K3. m12 over {BBB 2, CCC 3} -> BBB -1, CCC +1.
    # BBB momentum = mean(0, -1) = -0.5; CCC = mean(K3, 1).
    inputs = [
        _fi("AAA", m6=1, m12=None, prof=1, vol=1, value=1),
        _fi("BBB", m6=2, m12=2, prof=2, vol=2, value=2),
        _fi("CCC", m6=3, m12=3, prof=3, vol=3, value=3),
    ]
    scored = composite_scores(inputs, UniverseConfig())
    assert scored["AAA"].factors["momentum"] == pytest.approx(-K3)
    assert scored["BBB"].factors["momentum"] == pytest.approx(-0.5)
    assert scored["CCC"].factors["momentum"] == pytest.approx((K3 + 1) / 2)
    assert scored["AAA"].missing_factors == ()


def test_ticker_missing_two_factors_is_unscored():
    # Spec: a ticker missing >= 2 factors is unscored (composite None),
    # never given a neutral 0 that would read as "average".
    inputs = [
        _fi("AAA", m6=1, m12=1, prof=None, vol=1, value=None),
        _fi("BBB", m6=2, m12=2, prof=2, vol=2, value=2),
        _fi("CCC", m6=3, m12=3, prof=3, vol=3, value=3),
    ]
    scored = composite_scores(inputs, UniverseConfig())
    assert scored["AAA"].composite is None
    assert scored["AAA"].missing_factors == ("profitability", "value")
    assert scored["BBB"].composite is not None


def test_composite_scores_refuses_duplicate_tickers():
    with pytest.raises(ValueError):
        composite_scores([_fi("AAA", m6=1), _fi("AAA", m6=2)], UniverseConfig())


def test_compute_pool_stats_reports_raw_missing_fraction_per_factor():
    # 10 names; momentum counts as missing only when BOTH horizons are absent.
    inputs = [_fi(_sym(i), m6=1, m12=1, prof=1, vol=1, value=1) for i in range(10)]
    inputs[0] = _fi(_sym(0), m6=None, m12=1, prof=None, vol=1, value=1)  # momentum present via m12
    inputs[1] = _fi(_sym(1), m6=None, m12=None, prof=1, vol=1, value=None)
    stats = compute_pool_stats(inputs)
    assert stats.pool_size == 10
    assert stats.factor_missing_fraction == {
        "momentum": pytest.approx(0.1),
        "profitability": pytest.approx(0.1),
        "low_volatility": 0.0,
        "value": pytest.approx(0.1),
    }


def test_rank_tickers_breaks_ties_by_symbol_and_skips_unscored():
    board = {
        "BBB": ScoredTicker("BBB", "X", "X1", 1.0),
        "AAA": ScoredTicker("AAA", "X", "X1", 1.0),
        "CCC": ScoredTicker("CCC", "X", "X1", 2.0),
        "DDD": ScoredTicker("DDD", "X", "X1", None),
    }
    assert rank_tickers(board) == {"CCC": 1, "AAA": 2, "BBB": 3}


# --------------------------------------------------------------------------
# Selection: hysteresis
# --------------------------------------------------------------------------


def test_incumbent_ranked_inside_keep_band_stays():
    # 100 incumbents: ranks 1..99 plus rank 120. Rank 120 is outside the
    # top 100 but inside the 150 keep band -> stays. Every non-member ranked
    # <= 75 is already an incumbent, so nothing enters and nothing leaves.
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents([*range(1, 100), 120]))
    assert p.adds == () and p.drops == ()
    assert _rank_sym(120) in p.holds and len(p.active) == 100
    assert p.reasons[_rank_sym(120)] == "hold:rank_within_keep_band"


@pytest.mark.parametrize("rank, dropped", [(150, False), (151, True)])
def test_keep_band_boundary(rank, dropped):
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents([*range(1, 100), rank]))
    assert (_rank_sym(rank) in p.drops) is dropped
    if dropped:
        assert p.reasons[_rank_sym(rank)] == "drop:rank_outside_keep_band"
        assert len(p.active) == 99


def test_non_member_in_entry_band_for_only_one_review_does_not_enter():
    # Incumbents: ranks 1..100 except 60 -> 99 seats used, one free. The
    # non-member at rank 60 qualifies NOW but was rank 80 at the prior
    # review (> 75), so it has not been in the entry band two reviews in a
    # row and must not enter. No one else ranked <= 75 is a non-member.
    board = _board(200)
    prior = _prior_same(board)
    prior[_rank_sym(60)] = 80
    p = _select(board, prior, _incumbents([r for r in range(1, 101) if r != 60]))
    assert p.adds == ()
    assert p.reasons[_rank_sym(60)] == "skip:not_in_entry_band_prior_review"
    assert len(p.active) == 99


def test_non_member_absent_from_prior_review_does_not_enter():
    board = _board(200)
    prior = _prior_same(board)
    del prior[_rank_sym(60)]
    p = _select(board, prior, _incumbents([r for r in range(1, 101) if r != 60]))
    assert p.adds == ()


@pytest.mark.parametrize("prior_rank", [60, 75])
def test_non_member_in_entry_band_two_reviews_in_a_row_enters(prior_rank):
    # Same setup, but rank 60 now and <= 75 (60, or exactly 75) at the prior
    # review -> enters into the one free seat.
    board = _board(200)
    prior = _prior_same(board)
    prior[_rank_sym(60)] = prior_rank
    p = _select(board, prior, _incumbents([r for r in range(1, 101) if r != 60]))
    assert p.adds == (_rank_sym(60),)
    assert p.reasons[_rank_sym(60)] == "add:entry_band_two_reviews"
    assert len(p.active) == 100
    # A genuine add carries the evidence validate_proposal re-checks.
    assert p.prior_ranks[_rank_sym(60)] == prior_rank
    assert validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()) == []


def test_non_member_ranked_76_never_enters_even_with_room():
    # 91 incumbents at ranks 1..75 and 77..92; rank 76 qualified at the prior
    # review too, but 76 > 75 so it is outside the entry band. 9 free seats
    # stay empty rather than relaxing the band.
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents([*range(1, 76), *range(77, 93)]))
    assert _rank_sym(76) not in p.adds and p.adds == ()


# --------------------------------------------------------------------------
# Selection: tenure, missing data, hard fails
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tenure, dropped", [(1, False), (2, True)])
def test_min_tenure_protects_rank_based_exit(tenure, dropped):
    # Rank 151 is outside the keep band; with tenure 1 quarter (< 2) it is
    # held, with tenure 2 it is dropped.
    board = _board(200)
    inc = _incumbents(range(1, 100))
    inc[_rank_sym(151)] = Incumbent(tenure_quarters=tenure)
    p = _select(board, _prior_same(board), inc)
    assert (_rank_sym(151) in p.drops) is dropped
    if not dropped:
        assert p.reasons[_rank_sym(151)] == "hold:min_tenure"


def test_single_incumbent_with_missing_data_is_held_never_demoted():
    # Rank-50 incumbent absent from this review's scores entirely, and the
    # rank-51 incumbent present but unscored (composite None) -> both HELD.
    board = _board(200, drop={_rank_sym(50)})
    board[_rank_sym(51)] = dataclasses.replace(board[_rank_sym(51)], composite=None)
    p = _select(board, _prior_same(_board(200)), _incumbents(range(1, 101)))
    for ticker in (_rank_sym(50), _rank_sym(51)):
        assert ticker in p.holds and ticker not in p.drops
        assert p.reasons[ticker] == "hold:missing_data"
    assert len(p.active) == 100


def test_hard_fail_exits_always_apply_and_do_not_use_the_swap_budget():
    # Incumbents: ranks 1..92 (rank 1 with tenure 0) + 8 at ranks 161..168
    # with tenure 4. Hard fails: rank 1 (delisted) and rank 2 (fractional
    # ineligible). Expect: both hard fails dropped despite being rank 1/2
    # and despite rank 1's zero tenure; all 8 rank-based exits ALSO apply,
    # because hard-fail exits do not count toward the 8-swap cap.
    # Hard-failed names cannot re-enter, and every other rank <= 75 name is
    # an incumbent -> 100 - 10 = 90 active.
    board = _board(200)
    inc = _incumbents([*range(2, 93), *range(161, 169)])
    inc[_rank_sym(1)] = Incumbent(tenure_quarters=0)
    hard = {_rank_sym(1): "delisted", _rank_sym(2): "fractional_ineligible"}
    p = _select(board, _prior_same(board), inc, hard_fails=hard)
    assert set(p.hard_fail_drops) == {_rank_sym(1), _rank_sym(2)}
    assert set(p.drops) == {_rank_sym(1), _rank_sym(2), *(_rank_sym(r) for r in range(161, 169))}
    assert p.reasons[_rank_sym(1)] == "drop:hard_fail:delisted"
    assert p.adds == () and len(p.active) == 90
    assert validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()) == []


def test_hard_fail_vacancy_can_be_refilled_beyond_the_swap_budget():
    # Small config (max 3 active, swaps 1): incumbents AAA,BBB,CCC; BBB and
    # CCC hard-fail. Two vacancies from hard fails + 1 swap = 3 entries
    # allowed, but only 2 seats exist -> DDD and EEE enter (rank order),
    # FFF is skipped for lack of room.
    cfg = UniverseConfig(max_active=3, min_active=1, entry_rank=6, incumbent_keep_rank=6, max_swaps=1,
                         bench_min_rank=4, bench_max_rank=6)
    names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    board = {t: ScoredTicker(t, f"S{t}", f"I{t}", float(10 - i)) for i, t in enumerate(names)}
    inc = {t: Incumbent(4) for t in ("AAA", "BBB", "CCC")}
    hard = {"BBB": "delisted", "CCC": "pending_acquisition"}
    p = _select(board, _prior_same(board), inc, hard_fails=hard, config=cfg)
    assert p.adds == ("DDD", "EEE")
    assert p.reasons["FFF"] == "skip:no_room"


# --------------------------------------------------------------------------
# Selection: caps, size, swaps
# --------------------------------------------------------------------------


def _small_cfg(**kw) -> UniverseConfig:
    base = dict(max_active=4, min_active=1, entry_rank=5, incumbent_keep_rank=6, sector_cap=2, industry_cap=1,
                bench_min_rank=5, bench_max_rank=8)
    base.update(kw)
    return UniverseConfig(**base)


def _small_board(rows) -> dict[str, ScoredTicker]:
    # rows: (ticker, sector, industry) in rank order; composite = 10 - index.
    return {t: ScoredTicker(t, s, ind, float(10 - i)) for i, (t, s, ind) in enumerate(rows)}


def test_entries_respect_industry_and_sector_caps():
    # Caps: 2/sector, 1/industry, 4 seats, no incumbents. Entrants in rank order:
    #   AAA X/X1 -> add (X=1, X1=1)
    #   BBB X/X1 -> skip: industry X1 already at cap 1
    #   CCC X/X2 -> add (X=2)
    #   DDD X/X3 -> skip: sector X already at cap 2 (industry X3 is empty)
    #   EEE Y/Y1 -> add
    # Only 3 adds: no 4th eligible name ranked <= 5.
    board = _small_board([("AAA", "X", "X1"), ("BBB", "X", "X1"), ("CCC", "X", "X2"), ("DDD", "X", "X3"),
                          ("EEE", "Y", "Y1")])
    p = _select(board, _prior_same(board), {}, config=_small_cfg())
    assert p.adds == ("AAA", "CCC", "EEE")
    assert p.reasons["BBB"] == "skip:industry_cap"
    assert p.reasons["DDD"] == "skip:sector_cap"


def test_default_caps_bind_at_25_per_sector():
    # 30 non-members all in sector X, each in its own industry, ranked 1..30,
    # in the entry band at both reviews, no incumbents, swap budget opened
    # up so only the sector cap binds: exactly the top 25 enter.
    rows = [(_sym(i), "X", f"I{i}") for i in range(30)]
    board = _small_board(rows)
    for i, ticker in enumerate(board):
        board[ticker] = dataclasses.replace(board[ticker], composite=float(100 - i))
    p = _select(board, _prior_same(board), {}, config=UniverseConfig(max_swaps=100))
    assert p.adds == tuple(_sym(i) for i in range(25))
    assert p.reasons[_sym(25)] == "skip:sector_cap"


@pytest.mark.parametrize("tenure, expect_drop", [(4, True), (1, False)])
def test_incumbent_over_sector_cap_exits_unless_tenure_protected(tenure, expect_drop):
    # Sector cap 2, industry cap 4 (so only the sector cap binds). Incumbents
    # AAA r1 X, BBB r2 X, CCC r3 X, DDD r4 Y. Processing best-first, AAA and
    # BBB fill sector X; CCC is a cap-excess exit -- unless its tenure is
    # below 2 quarters, in which case it is held.
    board = _small_board([("AAA", "X", "X1"), ("BBB", "X", "X2"), ("CCC", "X", "X3"), ("DDD", "Y", "Y1")])
    inc = {"AAA": Incumbent(4), "BBB": Incumbent(4), "CCC": Incumbent(tenure), "DDD": Incumbent(4)}
    p = _select(board, _prior_same(board), inc, config=_small_cfg(industry_cap=4))
    assert ("CCC" in p.drops) is expect_drop
    assert p.reasons["CCC"] == ("drop:cap_excess" if expect_drop else "hold:min_tenure")


def test_never_more_than_max_active():
    # 98 incumbents at ranks 11..108 (all inside the keep band) leave 2
    # seats. Non-members at ranks 1..10 all qualify at both reviews; only the
    # two best (ranks 1, 2) enter, the rest are skipped for room.
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents(range(11, 109)))
    assert p.adds == (_rank_sym(1), _rank_sym(2))
    assert len(p.active) == 100
    assert all(p.reasons[_rank_sym(r)] == "skip:no_room" for r in range(3, 11))


def test_swap_cap_limits_rank_based_exits_worst_first():
    # Incumbents: ranks 1..88 + 12 at ranks 161..172 (tenure 4). All 12 are
    # outside the keep band, but only 8 rank-based exits are allowed per
    # review: the 8 WORST (ranks 165..172) go, 161..164 are deferred.
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents([*range(1, 89), *range(161, 173)]))
    assert set(p.drops) == {_rank_sym(r) for r in range(165, 173)}
    assert all(p.reasons[_rank_sym(r)] == "hold:exit_deferred_swap_cap" for r in range(161, 165))
    assert len(p.active) == 92


def test_swap_cap_limits_entries_into_empty_seats():
    # Migration phase-in: 80 incumbents (ranks 21..100), 20 empty seats, the
    # 20 non-members at ranks 1..20 all qualify -- but only 8 enter per
    # review (spec: first migration phases in at the same 8-swap cap).
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents(range(21, 101)))
    assert p.adds == tuple(_rank_sym(r) for r in range(1, 9))
    assert p.reasons[_rank_sym(9)] == "skip:swap_cap"


def test_ties_break_by_ticker_symbol():
    # BBB and AAA share a composite; one seat. AAA wins the tie (symbol order)
    # regardless of mapping insertion order.
    cfg = UniverseConfig(max_active=1, min_active=1, entry_rank=2, incumbent_keep_rank=2, bench_min_rank=2,
                         bench_max_rank=2)
    board = {"BBB": ScoredTicker("BBB", "X", "X1", 1.0), "AAA": ScoredTicker("AAA", "Y", "Y1", 1.0)}
    p = _select(board, {"AAA": 1, "BBB": 2}, {}, config=cfg)
    assert p.adds == ("AAA",) and p.reasons["BBB"] == "skip:no_room"


def test_selection_is_deterministic_regardless_of_input_order():
    board = _board(200)
    prior = _prior_same(board)
    inc = _incumbents([*range(1, 89), *range(161, 173)])
    a = _select(board, prior, inc)
    b = _select(dict(reversed(board.items())), dict(reversed(prior.items())), dict(reversed(inc.items())))
    assert proposal_to_dict(a) == proposal_to_dict(b)


def test_bench_is_ranks_101_to_250_excluding_active_and_hard_fails():
    # 300 scored names, incumbents at board positions 1..99 + 120 (120 is held
    # so it is active, not bench). Position 130 hard-fails (not an incumbent):
    # it is excluded BEFORE ranking, so every name below it moves up one rank
    # and board position 251 becomes rank 250. Position 100 is a non-member
    # outside both 101..250 and the entry band -> neither active nor bench.
    # Bench = positions 101..251 minus {120, 130} = 151 - 2 = 149 names.
    board = _board(300)
    inc = _incumbents([*range(1, 100), 120])
    p = _select(board, _prior_same(board), inc, hard_fails={_rank_sym(130): "delisted"})
    expected = tuple(_rank_sym(r) for r in range(101, 252) if r not in (120, 130))
    assert p.bench == expected and len(p.bench) == 149


# --------------------------------------------------------------------------
# Safeguards (fail closed)
# --------------------------------------------------------------------------


def test_clean_proposal_passes_every_safeguard():
    assert validate_proposal(_clean_proposal(), _prior_stats(), _fresh(), UniverseConfig()) == []


def test_more_than_max_rank_based_swaps_is_rejected():
    # Select with a loosened swap budget so 9 rank-based exits come through
    # (incumbents 1..91 + 9 at 161..169 -> 91 active, still >= 90), then
    # validate under the default 8-swap cap -> rejected on swaps alone.
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents([*range(1, 92), *range(161, 170)]),
                config=UniverseConfig(max_swaps=20))
    assert len(p.drops) == 9
    assert _codes(validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig())) == {"too_many_swaps"}


@pytest.mark.parametrize("pool_size, rejected", [(940, True), (950, False)])
def test_pool_coverage_drop_is_rejected(pool_size, rejected):
    # Spec: pool coverage < 95% of prior run -> reject. 950/1000 is exactly
    # 95% (passes); 940/1000 is 94% (rejected).
    p = _clean_proposal(pool_size=pool_size)
    codes = _codes(validate_proposal(p, _prior_stats(1000), _fresh(), UniverseConfig()))
    assert ("pool_coverage_drop" in codes) is rejected


def test_missing_prior_run_stats_fails_closed_but_first_review_is_explicit():
    p = _clean_proposal()
    assert _codes(validate_proposal(p, None, _fresh(), UniverseConfig())) == {"prior_run_stats_missing"}
    assert validate_proposal(p, PriorRunStats(pool_size=None, first_review=True), _fresh(), UniverseConfig()) == []


@pytest.mark.parametrize(
    "freshness, code",
    [
        (InputFreshness(date(2026, 10, 24), 4, date(2026, 10, 29)), "stale_quotes"),  # 4 sessions > 3
        (InputFreshness(date(2026, 10, 30), 0, date(2026, 10, 22)), "stale_edgar"),  # 8 days > 7
        (InputFreshness(date(2026, 10, 31), 0, date(2026, 10, 29)), "quote_date_after_as_of"),  # lookahead
        (InputFreshness(date(2026, 10, 30), True, date(2026, 10, 29)), "freshness_malformed"),
        (None, "freshness_missing"),
    ],
)
def test_stale_or_missing_inputs_are_rejected(freshness, code):
    assert code in _codes(validate_proposal(_clean_proposal(), _prior_stats(), freshness, UniverseConfig()))


def test_freshness_boundaries_pass():
    # Exactly 3 sessions and exactly 7 days are still fresh ("older than").
    ok = InputFreshness(date(2026, 10, 27), 3, date(2026, 10, 23))
    assert validate_proposal(_clean_proposal(), _prior_stats(), ok, UniverseConfig()) == []


@pytest.mark.parametrize("fraction, rejected", [(0.11, True), (0.10, False)])
def test_factor_missing_over_ten_percent_is_rejected(fraction, rejected):
    rates = {"momentum": 0.0, "profitability": fraction, "low_volatility": 0.0, "value": 0.0}
    p = _clean_proposal(factor_missing_fraction=rates)
    codes = _codes(validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()))
    assert ("factor_missing_too_high" in codes) is rejected


def test_missing_pool_stats_or_factor_rate_fails_closed():
    p = dataclasses.replace(_clean_proposal(), pool_stats=None)
    assert "pool_stats_missing" in _codes(validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()))
    rates = {"momentum": 0.0, "profitability": 0.0, "low_volatility": 0.0}  # value absent
    p = _clean_proposal(factor_missing_fraction=rates)
    assert "factor_missing_too_high" in _codes(validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()))


@pytest.mark.parametrize("size, code", [(89, "active_too_small"), (101, "active_too_large")])
def test_active_size_out_of_bounds_is_rejected(size, code):
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents(range(1, size + 1)))
    assert len(p.active) == size
    assert code in _codes(validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()))


def test_rank_based_drop_of_an_unranked_ticker_is_rejected():
    # A hand-edited or buggy proposal that demotes an incumbent with no rank
    # (missing data) must be refused: missing data means hold.
    p = _clean_proposal()
    victim = _rank_sym(5)
    ranks = {t: r for t, r in p.ranks.items() if t != victim}
    tampered = dataclasses.replace(
        p, ranks=ranks, drops=(victim,), holds=tuple(t for t in p.holds if t != victim),
        active=tuple(t for t in p.active if t != victim), reasons={**p.reasons, victim: "drop:rank_outside_keep_band"},
    )
    codes = _codes(validate_proposal(tampered, _prior_stats(), _fresh(), UniverseConfig()))
    assert "demoted_on_missing_data" in codes


def test_inconsistent_membership_is_rejected():
    p = _clean_proposal()
    tampered = dataclasses.replace(p, active=(*p.active, "ZZZ"))  # appears from nowhere
    assert "membership_inconsistent" in _codes(validate_proposal(tampered, _prior_stats(), _fresh(), UniverseConfig()))


def test_apply_safeguards_rejection_keeps_last_approved_list():
    board = _board(200)
    prior_active = _incumbents([*range(1, 92), *range(161, 170)])
    p = _select(board, _prior_same(board), prior_active, config=UniverseConfig(max_swaps=20))
    out = apply_safeguards(p, _prior_stats(), _fresh(), UniverseConfig())
    assert out.status == "rejected"
    assert out.recommended_active == tuple(sorted(prior_active))
    assert _codes(out.safeguard_failures) == {"too_many_swaps"}


def test_apply_safeguards_pass_is_pending_approval_not_approved():
    out = apply_safeguards(_clean_proposal(), _prior_stats(), _fresh(), UniverseConfig())
    assert out.status == "pending_approval"
    assert out.recommended_active == out.active and out.safeguard_failures == ()


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def test_proposal_serializes_with_a_stable_content_hash(tmp_path):
    p = apply_safeguards(_clean_proposal(), _prior_stats(), _fresh(), UniverseConfig())
    doc = proposal_to_dict(p)
    assert doc["review_id"] == "2026Q4-test"
    assert doc["as_of_date"] == "2026-10-30"
    assert doc["generated_at"] == "2026-10-31T12:00:00+00:00"
    assert doc["status"] == "pending_approval"
    assert doc["safeguards"] == {"passed": True, "failures": []}
    assert len(doc["content_sha256"]) == 64
    # The hash covers content, not the wall-clock generation stamp: the
    # same review regenerated later must hash identically ...
    later = dataclasses.replace(p, generated_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert proposal_to_dict(later)["content_sha256"] == doc["content_sha256"]
    # ... while any membership change must change it.
    changed = dataclasses.replace(p, bench=p.bench[1:])
    assert proposal_to_dict(changed)["content_sha256"] != doc["content_sha256"]

    path = tmp_path / "universe_proposal.json"
    written_hash = write_proposal(path, p)
    assert written_hash == doc["content_sha256"]
    loaded = json.loads(path.read_text())
    assert loaded == doc
    assert "NaN" not in path.read_text()


def test_rejected_proposal_serializes_failures():
    board = _board(200)
    p = _select(board, _prior_same(board), _incumbents([*range(1, 92), *range(161, 170)]),
                config=UniverseConfig(max_swaps=20))
    doc = proposal_to_dict(apply_safeguards(p, _prior_stats(), _fresh(), UniverseConfig()))
    assert doc["status"] == "rejected"
    assert doc["safeguards"]["passed"] is False
    assert [f["code"] for f in doc["safeguards"]["failures"]] == ["too_many_swaps"]
    assert doc["recommended_active"] == doc["prior_active"]


# --------------------------------------------------------------------------
# Review round 1 (independent review of PR #2100)
# --------------------------------------------------------------------------


def test_hard_fails_do_not_occupy_rank_slots():
    # Review finding 1: hard-failed names were ranked before removal, shifting
    # the entry/keep edges. Config: entry band 2, keep band 3. Raw order:
    #   AAA 1 (non-member, hard-failed)  CCC 2 (incumbent)
    #   BBB 3 (non-member, prior rank 2) DDD 4 (incumbent, tenure 4)
    # With AAA excluded BEFORE ranking: CCC 1, BBB 2, DDD 3 -> BBB is inside
    # the entry band (enters) and DDD inside the keep band (stays). With the
    # bug, BBB sat at 3 (> 2, no entry) and DDD at 4 (> 3, dropped).
    cfg = UniverseConfig(max_active=3, min_active=1, entry_rank=2, incumbent_keep_rank=3, bench_min_rank=4,
                         bench_max_rank=5)
    board = _small_board([("AAA", "W", "W1"), ("CCC", "X", "X1"), ("BBB", "Y", "Y1"), ("DDD", "Z", "Z1")])
    inc = {"CCC": Incumbent(4), "DDD": Incumbent(4)}
    p = _select(board, {"BBB": 2}, inc, hard_fails={"AAA": "delisted"}, config=cfg)
    assert "AAA" not in p.ranks
    assert p.ranks == {"CCC": 1, "BBB": 2, "DDD": 3}
    assert p.adds == ("BBB",) and p.drops == ()
    assert p.reasons["AAA"] == "skip:hard_fail"


def test_ineligible_tickers_separates_failed_floors_from_missing_data():
    result = filter_pool(
        [
            _cand("AAA"),
            _cand("CAP", market_cap=1e9),  # genuinely below the $2B floor
            _cand("MIX", market_cap=1e9, price=None),  # one real failure is enough
            _cand("GAP", price=None),  # only missing data -> not "ineligible"
            _cand("DUP"),
            _cand("DUP"),  # ambiguous rows -> missing data, not a verdict
        ],
        PoolThresholds(),
    )
    assert ineligible_tickers(result) == {
        "CAP": "market_cap_at_or_below_min",
        "MIX": "market_cap_at_or_below_min",
    }


def test_incumbent_that_leaves_the_pool_exits_even_inside_min_tenure():
    # Review finding 3: an incumbent that no longer clears a pool floor has
    # no score; it must exit, not be held as "missing data" forever. Leaving
    # the pool is a rule exit, not a rank-based one, so the 2-quarter tenure
    # protection (spec: "before any RANK-based exit") does not apply; it does
    # use the swap budget (it is not a hard fail).
    board = _board(200)
    gone = _rank_sym(250)  # not on the 200-name board at all
    inc = _incumbents(range(1, 100))
    inc[gone] = Incumbent(tenure_quarters=1)
    p = _select(board, _prior_same(board), inc, ineligible={gone: "market_cap_at_or_below_min"})
    assert p.drops == (gone,) and p.ineligible_drops == (gone,)
    assert p.reasons[gone] == "drop:left_pool:market_cap_at_or_below_min"
    assert validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()) == []


def test_pool_exits_use_the_swap_budget_ahead_of_rank_exits():
    # 9 pool-ineligible incumbents (unranked) + 2 rank-based exit candidates
    # (ranks 161, 162), 8-swap budget. Pool exits go first, in ticker order:
    # the 8 alphabetically-first pool exits leave, the 9th and both
    # rank-based candidates are deferred.
    board = _board(200)
    gone = [_sym(300 + i) for i in range(9)]
    inc = _incumbents([*range(1, 90), 161, 162])
    inc.update({t: Incumbent(4) for t in gone})
    p = _select(board, _prior_same(board), inc, ineligible=dict.fromkeys(gone, "dollar_volume_at_or_below_min"))
    assert set(p.drops) == set(gone[:8])
    for ticker in (gone[8], _rank_sym(161), _rank_sym(162)):
        assert p.reasons[ticker] == "hold:exit_deferred_swap_cap"


def test_missing_incumbent_not_marked_ineligible_is_still_held():
    board = _board(200, drop={_rank_sym(50)})
    p = _select(board, _prior_same(_board(200)), _incumbents(range(1, 101)), ineligible={})
    assert p.reasons[_rank_sym(50)] == "hold:missing_data"


def _one_add_proposal() -> Proposal:
    # Rank-60 non-member qualified at both reviews fills the one free seat.
    board = _board(200)
    return _select(board, _prior_same(board), _incumbents([r for r in range(1, 101) if r != 60]))


def _tamper_unscored_add(p):
    # Review finding 2's probe: swap the genuine add for a never-ranked symbol.
    real = p.adds[0]
    return dataclasses.replace(
        p, adds=("ZZZZ",), active=tuple(sorted({*p.holds, "ZZZZ"})),
        classification={**{t: c for t, c in p.classification.items() if t != real}, "ZZZZ": {"sector": "SEC0",
                                                                                          "industry": "IND0"}},
    )


def _tamper_rank(p):
    return dataclasses.replace(p, ranks={**p.ranks, p.adds[0]: 80})  # outside the entry band now


def _tamper_prior(p):
    return dataclasses.replace(p, prior_ranks={p.adds[0]: 80})  # outside the entry band last review


def _tamper_no_prior(p):
    return dataclasses.replace(p, prior_ranks={})


def _tamper_no_class(p):
    return dataclasses.replace(p, classification={t: c for t, c in p.classification.items() if t != p.adds[0]})


@pytest.mark.parametrize("tamper", [_tamper_unscored_add, _tamper_rank, _tamper_prior, _tamper_no_prior,
                                    _tamper_no_class])
def test_unqualified_add_is_rejected(tamper):
    p = _one_add_proposal()
    assert validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig()) == []
    codes = _codes(validate_proposal(tamper(p), _prior_stats(), _fresh(), UniverseConfig()))
    assert "add_not_qualified" in codes


def test_add_breaching_a_cap_is_rejected():
    # The rank-60 add lands in sector SEC9 (index 59 % 10), which holds 10
    # active names after the add. Validated under a 9/sector cap -> rejected;
    # under the default 25 it passes (previous test).
    p = _one_add_proposal()
    codes = _codes(validate_proposal(p, _prior_stats(), _fresh(), UniverseConfig(sector_cap=9)))
    assert "add_not_qualified" in codes


def test_hard_fail_drops_apply_even_when_the_proposal_is_rejected():
    # Review finding 3b: 91 incumbents, 2 of them hard-fail -> 89 active ->
    # rejected (active_too_small). Falling back to the full prior list would
    # keep buying a delisted name; the fallback must still drop hard fails.
    board = _board(200)
    inc = _incumbents(range(1, 92))
    hard = {_rank_sym(10): "delisted", _rank_sym(20): "pending_acquisition"}
    out = apply_safeguards(_select(board, _prior_same(board), inc, hard_fails=hard), _prior_stats(), _fresh(),
                           UniverseConfig())
    assert out.status == "rejected" and "active_too_small" in _codes(out.safeguard_failures)
    assert out.recommended_active == tuple(sorted(set(inc) - set(hard)))
    assert len(out.recommended_active) == 89


@pytest.mark.parametrize(
    "freshness, code",
    [
        # Review finding 4: age 0 but dated ten months back -> calendar ceiling.
        (InputFreshness(date(2026, 1, 1), 0, date(2026, 10, 29)), "stale_quotes_calendar"),
        # More sessions than calendar days elapsed is impossible -> malformed record.
        (InputFreshness(date(2026, 10, 28), 3, date(2026, 10, 29)), "freshness_inconsistent"),
    ],
)
def test_quote_age_is_cross_checked_against_quote_date(freshness, code):
    assert code in _codes(validate_proposal(_clean_proposal(), _prior_stats(), freshness, UniverseConfig()))
