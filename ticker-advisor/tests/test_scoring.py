"""Tests for scoring.py -- pure stdlib, no third-party deps needed."""

from scoring import TickerScore, classify, combine_scores, rank


def _health(sharpe=1.0, sortino=1.0, max_drawdown=-0.1, cagr=0.2, calmar=2.0, beta=1.0, alpha=0.05):
    return {
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "cagr": cagr,
        "calmar": calmar,
        "beta": beta,
        "alpha": alpha,
    }


def _backtest(ma_sharpe=1.0, rsi_sharpe=1.0):
    return {"ma_crossover": {"sharpe": ma_sharpe}, "rsi_reversion": {"sharpe": rsi_sharpe}}


def test_classify_bands():
    assert classify(1.0) == "ADD"
    assert classify(0.5) == "ADD"
    assert classify(0.49) == "WATCH"
    assert classify(-0.5) == "WATCH"
    assert classify(-0.51) == "REJECT"
    assert classify(None) == "WATCH"


def test_combine_scores_ranks_higher_metrics_higher():
    raw = {
        "STRONG": {
            "health": _health(sharpe=2.0, sortino=2.0, calmar=3.0, cagr=0.3, alpha=0.1),
            "backtest": _backtest(ma_sharpe=1.5, rsi_sharpe=1.2),
            "sentiment": {"score": 0.6},
        },
        "WEAK": {
            "health": _health(sharpe=-1.0, sortino=-1.0, max_drawdown=-0.5, cagr=-0.1, calmar=-0.2, alpha=-0.1),
            "backtest": _backtest(ma_sharpe=-0.5, rsi_sharpe=-0.3),
            "sentiment": {"score": -0.4},
        },
    }

    scores = combine_scores(raw)

    assert scores["STRONG"].composite is not None and scores["WEAK"].composite is not None
    assert scores["STRONG"].composite > scores["WEAK"].composite
    assert scores["STRONG"].classification == "ADD"
    assert scores["WEAK"].classification == "REJECT"
    ranked = rank(scores)
    assert [s.ticker for s in ranked] == ["STRONG", "WEAK"]


def test_missing_signal_excluded_not_defaulted_to_zero():
    raw = {
        "ONLY_HEALTH": {"health": _health(), "backtest": None, "sentiment": None},
        "ALL_THREE": {"health": _health(), "backtest": _backtest(), "sentiment": {"score": 0.5}},
    }

    scores = combine_scores(raw)

    assert scores["ONLY_HEALTH"].sub_scores["backtest"] is None
    assert scores["ONLY_HEALTH"].sub_scores["sentiment"] is None
    assert any("backtest signal unavailable" in note for note in scores["ONLY_HEALTH"].rationale)
    assert any("sentiment signal unavailable" in note for note in scores["ONLY_HEALTH"].rationale)


def test_combine_scores_records_partially_excluded_health_component():
    # _blend() silently averages whichever components ARE usable -- when
    # Calmar is NaN (zero drawdown, common in practice) for fewer than 2
    # candidates in the batch, calmar's z-score is excluded entirely
    # (fewer than 2 usable values) while sharpe still normalizes fine, so
    # sub_scores["health"] comes back non-None from sharpe alone. The
    # `value is None` branch never fires, so the omission previously went
    # unrecorded even though scores.md documents health as a blend of both
    # named components (Codex review on PR #403: "Record partially
    # excluded signal components").
    # Two OTHER candidates with usable Calmar so it clears the z-score's
    # own "fewer than 2 usable values" exclusion floor -- otherwise Calmar
    # would be excluded for every candidate, not just ZERO_DRAWDOWN, and
    # wouldn't isolate the partial-component case this test targets.
    raw = {
        "ZERO_DRAWDOWN": {"health": _health(sharpe=1.0, calmar=float("nan")), "backtest": None, "sentiment": None},
        "OTHER1": {"health": _health(sharpe=0.5, calmar=2.0), "backtest": None, "sentiment": None},
        "OTHER2": {"health": _health(sharpe=0.8, calmar=3.0), "backtest": None, "sentiment": None},
    }

    scores = combine_scores(raw)

    assert scores["ZERO_DRAWDOWN"].sub_scores["health"] is not None
    assert any(
        "health sub-score computed from sharpe only" in note and "calmar" in note
        for note in scores["ZERO_DRAWDOWN"].rationale
    )
    # The candidates with a usable Calmar must NOT get a spurious partial-
    # component note -- both their components contributed.
    assert not any("sub-score computed from" in note for note in scores["OTHER1"].rationale)
    assert not any("sub-score computed from" in note for note in scores["OTHER2"].rationale)
    # At least 2 OTHER candidates have a usable calmar, so THIS candidate's
    # own missing value is the reason, not batch-wide sparsity (Codex
    # review on PR #403: "Report the actual reason a component was
    # excluded").
    assert any(
        "no usable value for this candidate" in note and "calmar" in note
        for note in scores["ZERO_DRAWDOWN"].rationale
    )


def test_combine_scores_reports_batch_wide_sparsity_reason_when_applicable():
    # Only ONE candidate anywhere has a usable Calmar -- _zscore() excludes
    # it for EVERY candidate (fewer than 2 usable values batch-wide), not
    # just the ones missing it individually. Conflating this with a
    # candidate-specific missing value would misreport why THIS
    # candidate's calmar was excluded (Codex review on PR #403: "Report
    # the actual reason a component was excluded").
    raw = {
        "A": {"health": _health(sharpe=1.0, calmar=float("nan")), "backtest": None, "sentiment": None},
        "B": {"health": _health(sharpe=0.5, calmar=2.0), "backtest": None, "sentiment": None},
    }

    scores = combine_scores(raw)

    assert any(
        "fewer than 2 candidates in this batch have a usable value" in note and "calmar" in note
        for note in scores["A"].rationale
    )
    # B has a usable calmar of its own, but it's STILL excluded (batch-wide
    # sparsity), and B's rationale must say so too, not claim both
    # components contributed.
    assert any(
        "fewer than 2 candidates in this batch have a usable value" in note and "calmar" in note
        for note in scores["B"].rationale
    )


def test_all_signals_missing_yields_watch_not_reject_or_add():
    raw = {"NOTHING": {"health": None, "backtest": None, "sentiment": None}}

    scores = combine_scores(raw)

    assert scores["NOTHING"].composite is None
    assert scores["NOTHING"].classification == "WATCH"
    assert any("no usable signals" in note for note in scores["NOTHING"].rationale)


def test_nan_metrics_treated_as_unavailable():
    nan = float("nan")
    raw = {
        "NAN_HEALTH": {
            "health": _health(sharpe=nan, sortino=nan, max_drawdown=nan, cagr=nan, calmar=nan, alpha=nan),
            "backtest": None,
            "sentiment": None,
        }
    }

    scores = combine_scores(raw)

    assert scores["NAN_HEALTH"].sub_scores["health"] is None
    assert scores["NAN_HEALTH"].composite is None


def test_custom_weights_change_the_ranking():
    raw = {
        "SENTIMENT_LEADER": {
            "health": _health(sharpe=-1.0, sortino=-1.0, max_drawdown=-0.5, cagr=-0.1, calmar=-0.2, alpha=-0.1),
            "backtest": _backtest(ma_sharpe=-0.5, rsi_sharpe=-0.3),
            "sentiment": {"score": 0.9},
        },
        "HEALTH_LEADER": {
            "health": _health(sharpe=2.0, sortino=2.0, calmar=3.0, cagr=0.3, alpha=0.1),
            "backtest": _backtest(ma_sharpe=1.5, rsi_sharpe=1.2),
            "sentiment": {"score": -0.9},
        },
    }

    default_ranked = rank(combine_scores(raw))
    sentiment_weights = {"health": 0, "backtest": 0, "sentiment": 1}
    sentiment_heavy_ranked = rank(combine_scores(raw, weights=sentiment_weights))

    assert default_ranked[0].ticker == "HEALTH_LEADER"
    assert sentiment_heavy_ranked[0].ticker == "SENTIMENT_LEADER"


def test_single_candidate_batch_stays_unclassified_not_a_crash():
    # A batch of 1 has no cross-candidate spread to normalize against for
    # ANY metric, so every signal is excluded rather than fed to
    # CLASSIFICATION_BANDS as raw, wrong-scale values -- composite is None
    # and classification falls back to WATCH (never a raw-metric-driven
    # ADD/REJECT). See scoring._zscore's docstring; regression test for the
    # Antigravity/Codex reviews on PR #403.
    raw = {"ONLY": {"health": _health(), "backtest": _backtest(), "sentiment": {"score": 0.5}}}

    scores = combine_scores(raw)

    assert scores["ONLY"].composite is None
    assert scores["ONLY"].classification == "WATCH"
    assert any("excluded" in note for note in scores["ONLY"].rationale)


def test_metric_present_but_batch_too_small_has_distinct_rationale():
    # Sentiment is present for both tickers (batch size 2, normalizes fine),
    # but backtest is present for only ONE of the two -- that specific
    # signal should be excluded for BOTH tickers (nothing to normalize
    # against) with a rationale distinguishing "present but too sparse to
    # normalize" from "never computed at all."
    raw = {
        "HAS_BACKTEST": {"health": _health(), "backtest": _backtest(), "sentiment": {"score": 0.5}},
        "NO_BACKTEST": {"health": _health(), "backtest": None, "sentiment": {"score": -0.5}},
    }

    scores = combine_scores(raw)

    assert scores["HAS_BACKTEST"].sub_scores["backtest"] is None
    assert any(
        "present but excluded" in note and "backtest" in note for note in scores["HAS_BACKTEST"].rationale
    )
    assert any(
        "signal unavailable" in note and "backtest" in note for note in scores["NO_BACKTEST"].rationale
    )


def test_infinite_metric_excluded_not_fed_to_pstdev():
    # Two finite candidates so there's still enough spread left for the
    # finite ones to normalize against each other once INFINITE's inf
    # values are filtered out -- proves isfinite() exclusion, not just "the
    # whole batch became too small to normalize."
    raw = {
        "INFINITE": {
            "health": _health(sharpe=float("inf"), calmar=float("inf")),
            "backtest": None,
            "sentiment": None,
        },
        "NORMAL_A": {"health": _health(sharpe=1.0, calmar=2.0), "backtest": None, "sentiment": None},
        "NORMAL_B": {"health": _health(sharpe=-1.0, calmar=1.0), "backtest": None, "sentiment": None},
    }

    scores = combine_scores(raw)  # must not raise

    assert scores["INFINITE"].sub_scores["health"] is None
    assert scores["NORMAL_A"].sub_scores["health"] is not None
    assert scores["NORMAL_B"].sub_scores["health"] is not None


def test_near_identical_metrics_do_not_produce_extreme_zscores():
    # Values that differ only by floating-point summation noise (not
    # bit-identical, but effectively so) must still be treated as "no real
    # spread" -- pstdev's exact `== 0` check misses this and would divide by
    # a micro-epsilon stdev, producing artificially huge (>1000) z-scores
    # instead of 0.0 (Antigravity review on PR #403: "Apply Standard
    # Deviation Epsilon Threshold").
    raw = {
        "A": {"health": _health(sharpe=1.0 + 1e-13, calmar=2.0), "backtest": None, "sentiment": None},
        "B": {"health": _health(sharpe=1.0, calmar=2.0 + 1e-13), "backtest": None, "sentiment": None},
    }

    scores = combine_scores(raw)

    assert scores["A"].sub_scores["health"] == 0.0
    assert scores["B"].sub_scores["health"] == 0.0


def test_scale_dominant_metric_does_not_drown_out_the_other():
    # WORST_SHARPE has by far the worst Sharpe of the three but an outsized
    # raw Calmar (a much larger natural scale than Sharpe typically has).
    # Averaging RAW sharpe+calmar first (the old behavior) would let that
    # Calmar outlier drag its blended score to the top DESPITE the worst
    # Sharpe: (-3 + 50) / 2 = 23.5, vs BEST_SHARPE's (2 + 1) / 2 = 1.5.
    # Z-scoring sharpe and calmar independently before blending should not
    # let that happen.
    raw = {
        "WORST_SHARPE": {"health": _health(sharpe=-3.0, calmar=50.0), "backtest": None, "sentiment": None},
        "BEST_SHARPE": {"health": _health(sharpe=2.0, calmar=1.0), "backtest": None, "sentiment": None},
        "MIDDLE": {"health": _health(sharpe=1.0, calmar=1.5), "backtest": None, "sentiment": None},
    }

    scores = combine_scores(raw)

    best_health = scores["BEST_SHARPE"].sub_scores["health"]
    worst_health = scores["WORST_SHARPE"].sub_scores["health"]
    assert best_health is not None and worst_health is not None
    assert best_health > worst_health


def test_raw_errors_surface_in_rationale():
    # score_candidate() (advise_tickers.py) records exception messages in
    # raw["errors"] -- combine_scores() must fold those into rationale so a
    # reader of scores.md sees WHY a signal is missing, not just THAT it's
    # missing.
    raw = {
        "FLAKY": {
            "health": _health(),
            "backtest": None,
            "sentiment": None,
            "errors": ["backtest signal failed: vectorbt raised ValueError"],
        }
    }

    scores = combine_scores(raw)

    assert any("backtest signal failed: vectorbt raised ValueError" in note for note in scores["FLAKY"].rationale)


def test_missing_errors_key_does_not_crash():
    raw = {"NO_ERRORS_KEY": {"health": _health(), "backtest": None, "sentiment": None}}

    combine_scores(raw)  # must not raise (no "errors" key at all)


def test_rank_puts_no_composite_candidates_last():
    scores = {
        "A": TickerScore(ticker="A", raw={}, sub_scores={}, composite=1.0, classification="ADD"),
        "B": TickerScore(ticker="B", raw={}, sub_scores={}, composite=None, classification="WATCH"),
        "C": TickerScore(ticker="C", raw={}, sub_scores={}, composite=-1.0, classification="REJECT"),
    }

    ordered = [s.ticker for s in rank(scores)]

    assert ordered == ["A", "C", "B"]
