"""Tests for analysis.py: prose generated strictly from measured numbers."""

from __future__ import annotations

import analysis


def _report():
    close_block = {
        "n": 1000,
        "mae": 0.0123,
        "rmse": 0.02,
        "mae_price": 1.15,
        "pinball": {"p10": 0.001, "p50": 0.006, "p90": 0.001},
        "coverage": 0.79,
        "coverage_raw": 0.71,
        "band_width": 0.05,
    }
    return {
        "models": ["gbm_quantile", "persistence"],
        "nominal_coverage": 0.8,
        "n_scored": {"gbm_quantile": 1000, "persistence": 1000},
        "by_regime": {
            "all": {"gbm_quantile": {"close": dict(close_block)}},
            "covid": {"gbm_quantile": {"close": dict(close_block, mae=0.03, coverage=0.7)}},
            "other": {"gbm_quantile": {"close": dict(close_block, mae=0.01, coverage=0.82)}},
        },
        "range_hit": {"gbm_quantile": {"close_in_range": 0.66, "open_in_range": 0.71, "n": 1000}},
        "skill_vs_persistence": {"gbm_quantile": {"open": 0.05, "high": 0.04, "low": 0.06, "close": 0.05}},
        "market": {
            "gbm_quantile": {"aggregate_close_mae": 0.004, "error_variance_market_fraction": 0.62, "n_days": 250}
        },
        "universe": {"n_tickers": 3, "description": "demo archive"},
    }


def test_analysis_quotes_the_measured_numbers():
    text = analysis.render_analysis(_report())
    assert "79.0%" in text  # coverage
    assert "71.0%" in text  # raw coverage
    assert "66.0%" in text  # close-in-range
    assert "demo archive" in text
    assert "edges out" in text  # 5% mean skill verdict
    assert "mostly market-wide" in text  # 62% shared error variance
    assert "covid" in text and "other" in text  # worst / best regimes


def test_analysis_health_section_and_retrain_suggestion():
    health = {
        "status": "DEGRADED",
        "modes": {
            "time": {"baseline_mae_close": 0.005, "current_mae_close": 0.02, "degraded": True},
            "market": {"baseline_mae_close": 0.006, "current_mae_close": 0.007, "degraded": False},
        },
    }
    text = analysis.render_analysis(_report(), health=health)
    assert "Encoder health: DEGRADED" in text
    assert "--retrain" in text


def test_analysis_ticker_rankings():
    skill = {"AAA": 0.10, "BBB": 0.02, "CCC": -0.04, "DDD": 0.06, "EEE": -0.01}
    text = analysis.render_analysis(_report(), per_ticker_skill=skill)
    assert "AAA (10.0%)" in text
    assert "CCC (-4.0%)" in text


def test_analysis_ticker_rankings_do_not_overlap_with_few_tickers():
    # Regression (Antigravity review): with fewer than 6 ranked tickers,
    # `ranked[:3]` and `ranked[-3:]` used to share entries, double-listing
    # the same ticker as both "Highest" and "Lowest" skill.
    skill = {"AAA": 0.10, "BBB": 0.06, "CCC": 0.02, "DDD": -0.04}
    text = analysis.render_analysis(_report(), per_ticker_skill=skill)
    [tickers_line] = [line for line in text.splitlines() if line.startswith("**Tickers.**")]
    highest_part, lowest_part = tickers_line.split("Lowest: ")
    assert "BBB" in highest_part and "CCC" in highest_part
    assert "BBB" not in lowest_part
    assert "CCC" not in lowest_part
    assert "DDD" in lowest_part


def test_analysis_verdict_words():
    assert analysis._verdict(0.2) == "clearly beats"
    assert analysis._verdict(0.05) == "edges out"
    assert analysis._verdict(0.0) == "roughly matches"
    assert analysis._verdict(-0.1) == "currently loses to"


def test_analysis_honors_the_caller_selected_primary_model():
    # A multi-model run's --summary/--html views compute per-ticker skill
    # and health for the CALLER's --model selection; render_analysis must
    # describe that same model, not silently re-derive its own fixed
    # octopus-first preference (Codex review regression: "Honor the
    # selected model in generated analyses").
    text_default = analysis.render_analysis(_report())
    assert "from `gbm_quantile`" in text_default  # only gbm_quantile/persistence are in this report
    text_persistence = analysis.render_analysis(_report(), primary="persistence")
    assert "from `persistence`" in text_persistence
    # A model this report never scored falls back to the default preference.
    text_fallback = analysis.render_analysis(_report(), primary="octopus")
    assert "from `gbm_quantile`" in text_fallback


def test_analysis_survives_explicit_none_values_in_a_report():
    # dict.get(key, {}) only falls back to the default when the key is
    # ABSENT, not when it's present but explicitly None (e.g. a hand-
    # edited or older-schema report.json) -- the chained .get() on that
    # None used to raise AttributeError instead of degrading gracefully
    # (Antigravity review regression: "Robustness: Potential AttributeError
    # on Explicit None Values").
    report = dict(
        _report(),
        n_scored=None,
        by_regime=None,
        skill_vs_persistence=None,
        range_hit=None,
        market=None,
    )
    text = analysis.render_analysis(report)
    assert "**What this is.**" in text


def test_analysis_handles_a_report_with_no_scored_forecasts():
    # A valid run that has only pending forecasts (nothing has matured into
    # a scored day yet) reports an empty model list; render_analysis must
    # not index into it (Codex review regression: IndexError on report["models"][0]).
    report = {
        "models": [],
        "n_scored": {},
        "by_regime": {},
        "range_hit": {},
        "skill_vs_persistence": {},
        "market": {},
        "universe": {"n_tickers": 3, "description": "demo archive"},
    }
    text = analysis.render_analysis(report)
    assert "no forecast has matured" in text.lower()
    assert "3" in text
    assert analysis._primary_model(report) == "?"
