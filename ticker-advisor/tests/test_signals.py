"""Tests for signals.py.

Health-metric math, MA-crossover, and RSI-reversion signal generation only
need pandas/numpy/scipy, so those run unconditionally. Only the full
vectorbt-backed backtest is importorskip-guarded, so this file degrades
gracefully (skips, doesn't error at collection) in an environment that only
installed the lightweight half of requirements.txt.
"""

import math
import sys
import types
from unittest import mock

import pandas as pd
import pytest
from signals import (
    _HEALTH_METRIC_KEYS,
    MIN_OBSERVATIONS,
    _extract_filing_snippet,
    _sec_filing_document_url,
    _wilder_rsi,
    generate_ma_crossover_signals,
    strategy_health_score,
)


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2023-01-02", periods=n)


def test_strategy_health_score_below_min_observations_is_all_nan():
    returns = pd.Series([0.01, -0.01], index=_dates(2))

    metrics = strategy_health_score(returns)

    assert set(metrics.keys()) == set(_HEALTH_METRIC_KEYS)
    assert all(math.isnan(value) for value in metrics.values())


def test_strategy_health_score_positive_drift_has_positive_sharpe_and_cagr():
    n = 300
    # Alternating between two positive values -- real variance (so Sharpe is
    # finite, not NaN'd by the zero-variance guard) while every day is still
    # a gain, so cumulative return is monotonically non-decreasing and
    # max_drawdown stays exactly 0.0. A genuinely CONSTANT series here would
    # be numerically degenerate: pandas' .std() of a bit-identical series can
    # come back as a tiny nonzero epsilon (~1e-19) rather than an exact 0.0,
    # which used to produce an absurd ~1e17 "Sharpe" instead of NaN (Copilot
    # review on PR #403, and the _sharpe fix it led to -- a narrower version
    # of this same test-data change was independently applied via GitHub's
    # Copilot Autofix in beb13e3, superseded here since it doesn't fix
    # _sharpe's actual zero-variance bug, only sidesteps this one test).
    returns = pd.Series([0.0015 if i % 2 == 0 else 0.0005 for i in range(n)], index=_dates(n))

    metrics = strategy_health_score(returns)

    assert metrics["sharpe"] > 0
    assert not math.isnan(metrics["sharpe"])
    assert metrics["cagr"] > 0
    assert metrics["max_drawdown"] == 0.0  # every day is a gain, never a drawdown
    assert math.isnan(metrics["beta"])  # no benchmark supplied
    assert math.isnan(metrics["alpha"])


def test_strategy_health_score_with_benchmark_computes_beta_alpha():
    n = 300
    index = _dates(n)
    # Alternating +/- pattern so the benchmark has real variance (a constant
    # benchmark has no slope to fit -- see _beta_alpha's ValueError guard).
    benchmark = pd.Series([0.001 if i % 2 == 0 else -0.0004 for i in range(n)], index=index)
    returns = benchmark * 2.0  # exactly 2x the benchmark every day -> beta == 2

    metrics = strategy_health_score(returns, benchmark)

    assert metrics["beta"] == pytest.approx(2.0, abs=1e-6)


def test_strategy_health_score_flat_benchmark_degrades_to_nan_beta_not_a_crash():
    n = 300
    index = _dates(n)
    benchmark = pd.Series([0.0005] * n, index=index)  # zero variance
    returns = pd.Series([0.0007] * n, index=index)

    # The guard must run before scipy. Relying on linregress to reject this
    # input is version-dependent: some releases return a zero slope with a
    # precision-loss warning instead of raising ValueError.
    with mock.patch("scipy.stats.linregress", side_effect=AssertionError("regression must not run")):
        metrics = strategy_health_score(returns, benchmark)

    assert math.isnan(metrics["beta"])
    assert math.isnan(metrics["alpha"])


def test_strategy_health_score_constant_returns_sharpe_is_nan_not_a_huge_outlier():
    # A truly constant returns series has zero real variance for Sharpe's
    # denominator -- this must degrade to NaN, not a floating-point-noise
    # -driven outlier (~1e17 was observed before the fix, from pandas' .std()
    # of a bit-identical series not landing on an exact 0.0).
    n = 300
    returns = pd.Series([0.001] * n, index=_dates(n))

    metrics = strategy_health_score(returns)

    assert math.isnan(metrics["sharpe"])


def test_strategy_health_score_drawdown_is_negative_when_price_falls():
    n = MIN_OBSERVATIONS + 5
    returns = pd.Series([-0.05] * n, index=_dates(n))

    metrics = strategy_health_score(returns)

    assert metrics["max_drawdown"] < 0


def test_max_drawdown_includes_the_first_period_loss():
    # A single -10% period must show up as a -10% drawdown -- not 0%, which
    # is what cummax() reports if the running peak is only ever taken from
    # the ALREADY-reduced cumulative series with no 1.0 starting anchor
    # (Codex review on PR #403: "Include the initial capital in drawdown
    # calculation").
    from signals import _max_drawdown

    returns = pd.Series([-0.10], index=_dates(1))

    assert _max_drawdown(returns) == pytest.approx(-0.10)


def test_max_drawdown_still_correct_when_recovering_above_start():
    from signals import _max_drawdown

    # Down 20%, then up well past the original starting equity -- the worst
    # point (the -20% trough) must still be reported, not overwritten by the
    # later recovery.
    returns = pd.Series([-0.20, 0.50], index=_dates(2))

    assert _max_drawdown(returns) == pytest.approx(-0.20)


def test_generate_ma_crossover_signals_fires_on_a_clean_golden_cross():
    # Flat-then-rising close: a short/long MA pair should cross exactly once.
    flat = [100.0] * 20
    rising = [100.0 + i for i in range(1, 61)]
    close = pd.Series(flat + rising, index=_dates(len(flat) + len(rising)))

    entries, exits = generate_ma_crossover_signals(close, fast_window=5, slow_window=20)

    assert entries.dtype == bool
    assert exits.dtype == bool
    assert entries.sum() >= 1
    assert not (entries & exits).any()  # never both true on the same day


def test_generate_ma_crossover_signals_no_crossover_on_flat_series():
    close = pd.Series([100.0] * 250, index=_dates(250))

    entries, exits = generate_ma_crossover_signals(close, fast_window=5, slow_window=20)

    assert entries.sum() == 0
    assert exits.sum() == 0


def test_generate_rsi_reversion_signals_shapes():
    from signals import generate_rsi_reversion_signals

    close = pd.Series([100.0 + (i % 10) - 5 for i in range(120)], index=_dates(120))

    entries, exits = generate_rsi_reversion_signals(close)

    assert len(entries) == len(close)
    assert len(exits) == len(close)
    assert entries.dtype == bool
    assert exits.dtype == bool


def test_generate_rsi_reversion_signals_enters_on_recovery_not_the_dip():
    # Entry must fire on the bounce OUT of oversold (RSI crosses back above
    # rsi_low), not on the dip INTO oversold -- matches
    # trading-strategies/backtest.py's reference implementation (Codex
    # review on PR #403: "Enter RSI positions on the recovery cross").
    from signals import _wilder_rsi, generate_rsi_reversion_signals

    # A sharp drop (drives RSI below 30) followed by a recovery leg.
    drop = [100.0 - i * 2 for i in range(20)]
    recovery = [drop[-1] + i for i in range(1, 30)]
    close = pd.Series(drop + recovery, index=_dates(len(drop) + len(recovery)))

    rsi = _wilder_rsi(close, period=14)
    entries, _exits = generate_rsi_reversion_signals(close, rsi_period=14, rsi_low=30.0, rsi_high=70.0)

    entry_days = entries[entries].index
    assert len(entry_days) >= 1
    for day in entry_days:
        # On an entry day, RSI must be AT/ABOVE rsi_low with the PRIOR day
        # below it -- i.e. crossing up out of oversold, not down into it.
        position = close.index.get_loc(day)
        assert position > 0
        assert rsi.iloc[position] >= 30.0
        assert rsi.iloc[position - 1] < 30.0


def test_wilder_rsi_seeds_first_average_with_simple_period_mean():
    # Wilder's own recurrence seeds the FIRST smoothed average with the
    # plain mean of the initial `period` gains/losses -- not an EMA that
    # starts recursing from the very first delta (Codex review on PR #403:
    # "Seed Wilder averages with the initial period mean"). Hand-computed
    # for this 6-close, period=5 series with mixed early gains/losses:
    # gains [2,0,2,0,5] mean 1.8, losses [0,1,0,4,0] mean 1.0,
    # RS=1.8, RSI = 100 - 100/(1+1.8) = 64.285714...
    close = pd.Series([100.0, 102.0, 101.0, 103.0, 99.0, 104.0], index=_dates(6))

    rsi = _wilder_rsi(close, period=5)

    assert rsi.iloc[:5].isna().all()  # not enough data to seed before index `period`
    assert rsi.iloc[5] == pytest.approx(64.28571428571428)


def test_wilder_rsi_bounded_between_0_and_100():
    from signals import _wilder_rsi

    close = pd.Series([100.0 + (i % 10) - 5 for i in range(120)], index=_dates(120))

    rsi = _wilder_rsi(close)

    valid = rsi.dropna()
    assert (valid >= 0).all()
    assert (valid <= 100).all()


def test_wilder_rsi_is_100_when_no_losses():
    from signals import _wilder_rsi

    close = pd.Series([100.0 + i for i in range(30)], index=_dates(30))  # strictly increasing

    rsi = _wilder_rsi(close, period=14)

    assert rsi.dropna().iloc[-1] == pytest.approx(100.0)


def test_portfolio_metrics_shifts_entries_and_exits_by_one_bar(monkeypatch):
    # entries/exits are computed from day T's close -- pairing them with
    # THAT SAME day's close in Portfolio.from_signals fills an unrealizable
    # trade and inflates the backtest Sharpe fed into candidate rankings
    # (Codex review on PR #403: "Execute close-derived signals on the
    # following bar"). Captures what's actually passed to
    # vbt.Portfolio.from_signals rather than trusting a full vectorbt run.
    pytest.importorskip("vectorbt")
    import vectorbt as vbt  # type: ignore
    from signals import _portfolio_metrics

    close = pd.Series([100.0, 101.0, 102.0, 103.0], index=_dates(4))
    entries = pd.Series([True, False, False, False], index=close.index)
    exits = pd.Series([False, False, True, False], index=close.index)
    captured = {}

    class _FakeTrades:
        def count(self):
            return 0

        def win_rate(self):
            return 0.0

    class _FakePortfolio:
        # Portfolio-shaped: _portfolio_metrics accesses these as bound,
        # zero-arg callables (matching vectorbt's real Portfolio API), not
        # a bare object() -- the previous fake made this test pass only
        # because the real vectorbt/Numba stack isn't installed here to
        # actually reach these attribute accesses; against the documented
        # full requirements.txt it raises AttributeError before the
        # assertions below run (Codex review on PR #403: "Return a
        # portfolio-shaped fake from the shift test").
        trades = _FakeTrades()

        def sharpe_ratio(self):
            return 1.0

        def sortino_ratio(self):
            return 1.0

        def max_drawdown(self):
            return -0.1

        def calmar_ratio(self):
            return 1.0

        def total_return(self):
            return 0.05

    def _fake_from_signals(close_arg, entries_arg, exits_arg, **kwargs):
        captured["entries"] = list(entries_arg)
        captured["exits"] = list(exits_arg)
        return _FakePortfolio()

    monkeypatch.setattr(vbt.Portfolio, "from_signals", staticmethod(_fake_from_signals))

    metrics = _portfolio_metrics(close, entries, exits, commission=0.001, slippage=0.0, init_cash=10_000.0)

    assert captured["entries"] == [False, True, False, False]
    assert captured["exits"] == [False, False, False, True]
    assert metrics["total_trades"] == 0
    assert metrics["sharpe"] == pytest.approx(1.0)


def test_backtest_score_returns_both_strategies():
    pytest.importorskip("vectorbt")
    from signals import backtest_score

    close = pd.Series([100.0 + i * 0.1 for i in range(400)], index=_dates(400))

    result = backtest_score(close)

    assert set(result.keys()) == {"ma_crossover", "rsi_reversion"}
    for metrics in result.values():
        assert "sharpe" in metrics
        assert "total_trades" in metrics


def test_compute_ticker_sentiment_pure_math_no_torch_needed():
    from signals import compute_ticker_sentiment

    scored = [
        {"positive": 0.9, "negative": 0.05, "neutral": 0.05, "label": "positive", "confidence": 0.9},
        {"positive": 0.1, "negative": 0.8, "neutral": 0.1, "label": "negative", "confidence": 0.8},
    ]

    score = compute_ticker_sentiment(scored)

    assert score is not None
    assert -1.0 <= score <= 1.0


def test_compute_ticker_sentiment_empty_input_returns_none():
    from signals import compute_ticker_sentiment

    assert compute_ticker_sentiment([]) is None


def test_fetch_sec_filings_uses_injected_fetcher_no_network():
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {
                    "_source": {"display_names": ["ACME CORP (ACME)"], "file_type": "10-K"},
                    "_text": "ACME Corp reported strong quarterly results.",
                },
                {
                    "_source": {"display_names": ["ACME CORP (ACME)"], "file_type": "8-K"},
                    "_text": "ACME Corp announced a material event.",
                },
            ]
        }
    }

    filings = fetch_sec_filings("ACME", fetcher=lambda: fixture)

    assert len(filings) == 2
    assert filings[0]["ticker"] == "ACME"
    assert "10-K" in filings[0]["text"] or filings[0]["form"] == "10-K"


def test_fetch_sec_filings_degrades_to_empty_list_on_fetcher_error():
    from signals import fetch_sec_filings

    def broken_fetcher():
        raise ConnectionError("boom")

    assert fetch_sec_filings("ACME", fetcher=broken_fetcher) == []


def test_fetch_sec_filings_records_fetch_failure_when_errors_list_given():
    # A genuine fetch failure (a 403, a timeout, malformed JSON) previously
    # degraded to the SAME silent [] as "the request succeeded and found
    # nothing" -- indistinguishable downstream, so a real SEC outage could
    # change a candidate's classification with no explanation anywhere in
    # scores.md (Codex review on PR #403: "Preserve SEC request failures in
    # the report").
    from signals import fetch_sec_filings

    def broken_fetcher():
        raise ConnectionError("boom")

    errors: list[str] = []
    filings = fetch_sec_filings("ACME", fetcher=broken_fetcher, errors=errors)

    assert filings == []
    assert any("ACME" in message and "boom" in message for message in errors)


def test_fetch_sec_filings_records_error_for_non_dict_payload():
    # A response that parses as valid JSON but isn't a mapping at the top
    # level (null, a bare list, ...) would otherwise raise AttributeError
    # on `.get("hits")`, OUTSIDE the fetch exception handler -- and since
    # sentiment_score() fetches NewsAPI first and calls this function
    # second, that uncaught error would discard otherwise-available news
    # sentiment too (Codex review on PR #403: "Validate the top-level SEC
    # payload").
    from signals import fetch_sec_filings

    errors: list[str] = []
    filings = fetch_sec_filings("ACME", fetcher=lambda: None, errors=errors)

    assert filings == []
    assert any("ACME" in message for message in errors)


@pytest.mark.parametrize(
    "fixture",
    [
        {"hits": "error"},
        {"hits": {"hits": [None]}},
        {"hits": {"hits": [{"_source": None}]}},
        {"hits": {"hits": [{"_source": {"display_names": "not-a-list"}}]}},
        {"hits": {"hits": [{"_source": {"display_names": [123, None]}}]}},
    ],
)
def test_fetch_sec_filings_tolerates_malformed_nested_hit_shapes(fixture):
    # The top-level-payload check only guards `payload` itself -- the
    # nested "hits" containers can independently be a malformed shape
    # (a non-dict "hits", a null hit, a null "_source", a non-list/non-str
    # "display_names"), any of which would otherwise raise
    # AttributeError/TypeError, OUTSIDE the fetch exception handler, and
    # discard otherwise-valid news sentiment too since sentiment_score()
    # fetches SEC after NewsAPI (Codex review on PR #403: "Validate nested
    # SEC hit structures before dereferencing").
    from signals import fetch_sec_filings

    filings = fetch_sec_filings("ACME", fetcher=lambda: fixture)

    assert filings == []


def test_fetch_sec_filings_requires_issuer_match_in_display_names():
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {
                    "_source": {"display_names": ["Caterpillar Music LLC (BAND)"], "file_type": "10-K"},
                    "_text": "Caterpillar Music reported quarterly royalties.",
                },
                {
                    "_source": {"display_names": ["Caterpillar Inc. (CAT)"], "file_type": "10-K"},
                    "_text": "Caterpillar Inc. reported quarterly earnings.",
                },
            ]
        }
    }

    filings = fetch_sec_filings("CAT", fetcher=lambda: fixture)

    assert len(filings) == 1
    assert filings[0]["ticker"] == "CAT"


def test_fetch_sec_filings_matches_ticker_inside_multi_class_display_name():
    # A multi-class issuer's parenthesized field is a comma-separated list
    # ("Alphabet Inc. (GOOG, GOOGL)"), not a single ticker -- a bare
    # substring check for "(GOOG)" never matches inside "(GOOG, GOOGL)",
    # incorrectly discarding that issuer's legitimate filings for BOTH
    # share classes (Codex review on PR #403: "Parse ticker lists in SEC
    # display names").
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {
                    "_source": {"display_names": ["Alphabet Inc. (GOOG, GOOGL)"], "file_type": "10-K"},
                    "_text": "Alphabet Inc. reported quarterly earnings.",
                },
            ]
        }
    }

    goog_filings = fetch_sec_filings("GOOG", fetcher=lambda: fixture)
    googl_filings = fetch_sec_filings("GOOGL", fetcher=lambda: fixture)

    assert len(goog_filings) == 1
    assert len(googl_filings) == 1


def test_fetch_sec_filings_matches_dotted_ticker_against_hyphenated_display_name():
    # config/tickers.json uses the dotted share-class spelling (BRK.B);
    # EDGAR's parenthetical uses the hyphenated form (BRK-B) -- the same
    # provider-spelling mismatch _load_from_yfinance() already translates
    # at its own boundary. Without normalizing here too, this candidate's
    # legitimate filing fails the exact-membership check and its SEC
    # signal is silently discarded (Codex review on PR #403: "Normalize
    # share-class symbols before SEC issuer matching").
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {
                    "_source": {"display_names": ["Berkshire Hathaway Inc. (BRK-A, BRK-B)"], "file_type": "10-K"},
                    "_text": "Berkshire Hathaway reported quarterly earnings.",
                },
            ]
        }
    }

    filings = fetch_sec_filings("BRK.B", fetcher=lambda: fixture)

    assert len(filings) == 1


def test_fetch_sec_filings_rejects_hit_when_display_names_has_no_parseable_ticker():
    # Gating acceptance on whether a ticker could be PARSED out (rather
    # than on whether EFTS provided any display_names at all) let a hit
    # through unvalidated whenever none of the provided names happened to
    # contain a parenthesized ticker -- indistinguishable from the
    # genuinely-no-basis-to-validate case. For a common-word ticker like
    # "COIN" or "SHOP", an unrelated issuer that merely mentions the query
    # and supplies a tickerless display name would feed its text into
    # FinBERT and alter the composite (Codex review on PR #403: "Reject
    # SEC hits whose provided names contain no ticker").
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {"_source": {"display_names": ["Some Unrelated Fund LLC"], "file_type": "10-K"}},
            ]
        }
    }

    filings = fetch_sec_filings("COIN", fetcher=lambda: fixture)

    assert filings == []


def test_fetch_sec_filings_rejects_hit_with_no_display_names_at_all():
    # An entirely ABSENT "display_names" field used to be treated as "no
    # basis to validate, so accept" -- but this is a bare full-text search
    # (q=ticker), not an issuer-scoped one, so an unrelated filer whose
    # hit simply omits display_names could match on a common-word symbol
    # ("COIN", "SHOP", "CAT") and feed unrelated text into FinBERT under
    # the candidate's name (Antigravity review on PR #403: "Reject SEC
    # hits without issuer identity").
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {"_source": {"file_type": "10-K"}, "_text": "Some unrelated filing mentions coin collecting."},
            ]
        }
    }

    filings = fetch_sec_filings("COIN", fetcher=lambda: fixture)

    assert filings == []


def test_fetch_sec_filings_validates_text_field_types_before_combining():
    # A truthy but non-string "form_type"/"_text" (a nested object, a
    # number) previously either stored a non-string `text` that
    # _dedupe_items() later calls .strip() on (AttributeError) or got
    # passed to " ".join() (TypeError) -- either way escaping this
    # function's own "degrade, don't raise" contract, and since
    # sentiment_score() has already fetched NewsAPI by this point,
    # discarding otherwise-valid news sentiment too (Codex review on PR
    # #403: "Validate SEC text fields before combining sentiment"). The
    # non-string "_text" also means no usable snippet exists at all --
    # excluded rather than falling back to company-name-only metadata
    # text (Antigravity review on PR #403: "Do not score filing metadata
    # as filing sentiment").
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "display_names": ["Caterpillar Inc. (CAT)"],
                        "form_type": {"nested": "object"},
                    },
                    "_text": 12345,
                },
            ]
        }
    }

    filings = fetch_sec_filings("CAT", fetcher=lambda: fixture)  # must not raise

    assert filings == []


def test_fetch_sec_filings_applies_limit_after_issuer_filtering_not_before():
    # A relevant issuer hit at position 4 must still be found even though
    # `limit=3` -- slicing to the first 3 RAW hits before filtering would
    # discard three unrelated CAT-the-word hits and never reach the real
    # Caterpillar filing (Codex review on PR #403: "Filter issuer hits
    # before enforcing the filing limit").
    from signals import fetch_sec_filings

    unrelated = {
        "_source": {"display_names": ["Some Unrelated Fund (CATX)"], "file_type": "10-K"},
        "_text": "Some Unrelated Fund reported quarterly results.",
    }
    relevant = {
        "_source": {"display_names": ["Caterpillar Inc. (CAT)"], "file_type": "10-K"},
        "_text": "Caterpillar Inc. reported quarterly earnings.",
    }
    fixture = {"hits": {"hits": [unrelated, unrelated, unrelated, relevant]}}

    filings = fetch_sec_filings("CAT", limit=3, fetcher=lambda: fixture)

    assert len(filings) == 1
    assert "Caterpillar" in filings[0]["text"]


def test_fetch_sec_filings_handles_explicit_null_hits_field():
    # {"hits": null} makes payload.get("hits", {}) return None (the default
    # only applies when the key is MISSING) -- .get("hits", []) on that None
    # would raise AttributeError, escaping this function's own "degrades to
    # [] rather than raising" contract (Antigravity review on PR #403).
    from signals import fetch_sec_filings

    assert fetch_sec_filings("ACME", fetcher=lambda: {"hits": None}) == []


def test_fetch_sec_filings_handles_missing_hits_field():
    from signals import fetch_sec_filings

    assert fetch_sec_filings("ACME", fetcher=lambda: {}) == []


def test_fetch_sec_filings_excludes_hit_with_no_text_snippet():
    # Previously fell back to "COMPANY NAME (TICKER) 10-K" (issuer name +
    # form type) when no "_text" snippet was present at all -- that's
    # metadata ABOUT the filing, not sentiment-bearing filing CONTENT.
    # Scoring it with FinBERT isn't neutral-by-construction; it can dilute
    # otherwise-valid NewsAPI sentiment or manufacture a nonzero SEC
    # sentiment component when zero actual filing text was ever available
    # (Antigravity review on PR #403: "Do not score filing metadata as
    # filing sentiment").
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {"_source": {"display_names": ["ACME CORP (ACME)"], "file_type": "10-K"}},
            ]
        }
    }

    assert fetch_sec_filings("ACME", fetcher=lambda: fixture) == []


def test_fetch_sec_filings_fetches_document_when_text_snippet_absent():
    # A normal (non-highlighted) EFTS query returns NO "_text" field at
    # all -- requiring one discarded nearly every otherwise-valid issuer
    # hit and, with no NewsAPI key configured, left the SEC-only fallback
    # with no sentiment signal whatsoever (Codex review on PR #429: "Fetch
    # SEC filing content before requiring _text"). When the hit carries
    # enough EFTS metadata (_id, _source.ciks) to derive the filing
    # document's URL, fetch and use ITS content instead of discarding the
    # hit.
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {
                    "_id": "0001193125-26-012345:acme-20260101.htm",
                    "_source": {
                        "display_names": ["ACME CORP (ACME)"],
                        "file_type": "10-K",
                        "ciks": ["0000320193"],
                    },
                },
            ]
        }
    }
    fetched_urls = []

    def fake_document_fetcher(url):
        fetched_urls.append(url)
        return "<html><body><p>ACME Corp reported strong quarterly earnings.</p></body></html>"

    filings = fetch_sec_filings("ACME", fetcher=lambda: fixture, document_fetcher=fake_document_fetcher)

    assert fetched_urls == ["https://www.sec.gov/Archives/edgar/data/320193/000119312526012345/acme-20260101.htm"]
    assert len(filings) == 1
    assert "ACME Corp reported strong quarterly earnings." in filings[0]["text"]
    assert "<p>" not in filings[0]["text"]  # markup stripped


def test_fetch_sec_filings_excludes_hit_when_document_fetch_fails():
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {
                    "_id": "0001193125-26-012345:acme-20260101.htm",
                    "_source": {
                        "display_names": ["ACME CORP (ACME)"],
                        "file_type": "10-K",
                        "ciks": ["0000320193"],
                    },
                },
            ]
        }
    }

    def broken_document_fetcher(url):
        raise ConnectionError("boom")

    errors: list[str] = []
    filings = fetch_sec_filings(
        "ACME", fetcher=lambda: fixture, document_fetcher=broken_document_fetcher, errors=errors
    )

    assert filings == []
    assert any("ACME" in message and "boom" in message for message in errors)


def test_fetch_sec_filings_skips_document_fetch_without_derivable_url():
    # A hit with no "_id"/"_source.ciks" in the expected shape gives no
    # basis to construct a filing URL -- must degrade to excluding the hit
    # (the prior behavior) rather than guessing at a URL, and must not
    # even attempt a document fetch.
    from signals import fetch_sec_filings

    fixture = {
        "hits": {
            "hits": [
                {"_source": {"display_names": ["ACME CORP (ACME)"], "file_type": "10-K"}},
            ]
        }
    }

    def unexpected_document_fetcher(url):
        raise AssertionError("document fetch must not run without a derivable URL")

    filings = fetch_sec_filings("ACME", fetcher=lambda: fixture, document_fetcher=unexpected_document_fetcher)

    assert filings == []


@pytest.mark.parametrize(
    "hit,source",
    [
        ({}, {}),  # no "_id" at all
        ({"_id": "no-colon-here"}, {}),  # missing the ":filename" separator
        ({"_id": "0001193125-26-012345:acme.htm"}, {}),  # no "_source.ciks"
        ({"_id": "0001193125-26-012345:acme.htm"}, {"ciks": []}),  # empty ciks list
        ({"_id": "0001193125-26-012345:acme.htm"}, {"ciks": ["not-a-number"]}),  # non-numeric cik
        ({"_id": "not-numeric-accession:acme.htm"}, {"ciks": ["0000320193"]}),  # non-numeric accession
    ],
)
def test_sec_filing_document_url_returns_none_for_malformed_hits(hit, source):
    # No basis to construct a filing URL from any of these shapes -- must
    # return None so the caller degrades to excluding the hit, exactly as
    # before this fetch existed, rather than raising or guessing.
    assert _sec_filing_document_url(hit, source) is None


def test_sec_filing_document_url_builds_url_from_wellformed_hit():
    hit = {"_id": "0001193125-26-012345:acme-20260101.htm"}
    source = {"ciks": ["0000320193"]}

    url = _sec_filing_document_url(hit, source)

    assert url == "https://www.sec.gov/Archives/edgar/data/320193/000119312526012345/acme-20260101.htm"


def test_extract_filing_snippet_strips_tags_collapses_whitespace_and_caps_length():
    raw = "<html><body>  <p>Hello</p>\n\n<p>World</p>  " + ("x" * 10_000) + "</body></html>"

    snippet = _extract_filing_snippet(raw)

    assert snippet.startswith("Hello World")
    assert "<p>" not in snippet
    assert len(snippet) <= 4000


def test_extract_filing_snippet_returns_none_for_blank_or_non_string():
    assert _extract_filing_snippet("   <html>  </html>  ") is None
    assert _extract_filing_snippet(None) is None
    assert _extract_filing_snippet(12345) is None


def test_extract_filing_snippet_drops_head_style_script_and_hidden_xbrl_before_truncating():
    # A real inline-XBRL filing's <head> (CSS, metadata) and <ix:header>
    # (hidden tagged facts) routinely precede the visible narrative -- a
    # naive strip-tags-then-truncate can consume the whole snippet budget
    # on that boilerplate before reaching any actual prose, so FinBERT
    # would score filing metadata rather than filing CONTENT (Codex review
    # on PR #429: "Extract visible filing prose before truncating").
    raw = (
        "<html><head><style>.big{font-size:900px}</style>"
        + ("A" * 5000)  # boilerplate CSS/metadata bulk that must NOT survive
        + "</head>"
        "<ix:header>" + ("B" * 5000) + "</ix:header>"
        "<script>console.log('C');</script>"
        "<body><p>Actual issuer earnings narrative here.</p></body></html>"
    )

    snippet = _extract_filing_snippet(raw)

    assert snippet == "Actual issuer earnings narrative here."
    assert "A" * 10 not in snippet
    assert "B" * 10 not in snippet
    assert "console.log" not in snippet


def test_extract_filing_snippet_falls_back_to_whole_document_without_a_body_tag():
    # A plain-text filing (or a fragment EFTS returns with no <body> at
    # all) has nothing to narrow to -- the cleaned whole document is still
    # the best available text, not an empty result.
    raw = "Plain-text filing narrative with no HTML wrapper at all."

    snippet = _extract_filing_snippet(raw)

    assert snippet == raw


def test_fetch_sec_filings_scopes_the_default_request_to_days(monkeypatch):
    # SEC EDGAR's relevance ranking can otherwise surface arbitrarily old
    # filings even though `days` promises a recent window -- the default
    # (non-injected) fetcher must pass startdt/enddt (Codex review on PR
    # #403: "Apply the sentiment lookback to SEC filing searches").
    pytest.importorskip("requests")
    import requests
    from signals import fetch_sec_filings

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": {"hits": []}}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params or {})
        return _FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    fetch_sec_filings("ACME", days=7)

    assert "startdt" in captured
    assert "enddt" in captured


def test_fetch_sec_filings_anchors_date_range_to_as_of(monkeypatch):
    # A multi-candidate run spanning UTC midnight (plausible with a cold
    # multi-GB FinBERT download or a large candidate batch) would
    # otherwise give candidates before and after midnight different
    # windows purely from scoring order, since the default fetcher
    # recomputes "now" independently on every call -- as_of anchors every
    # candidate in the run to the SAME instant instead (Antigravity
    # review on PR #403: "Anchor sentiment lookbacks to the run
    # timestamp").
    pytest.importorskip("requests")
    from datetime import date

    import requests
    from signals import fetch_sec_filings

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": {"hits": []}}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params or {})
        return _FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    fetch_sec_filings("ACME", days=7, as_of=date(2020, 6, 15))

    assert captured["enddt"] == "2020-06-15"
    assert captured["startdt"] == "2020-06-08"


def test_fetch_sec_filings_no_date_range_when_days_not_given(monkeypatch):
    pytest.importorskip("requests")
    import requests
    from signals import fetch_sec_filings

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": {"hits": []}}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params or {})
        return _FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    fetch_sec_filings("ACME")

    assert "startdt" not in captured
    assert "enddt" not in captured


def test_fetch_news_headlines_returns_empty_without_api_key():
    from signals import fetch_news_headlines

    assert fetch_news_headlines("ACME", news_api_key=None) == []
    assert fetch_news_headlines("ACME", news_api_key="") == []


def test_fetch_news_headlines_uses_injected_fetcher_no_network():
    from signals import fetch_news_headlines

    fixture = {"articles": [{"title": "ACME beats earnings", "description": "Strong quarter"}]}

    headlines = fetch_news_headlines("ACME", news_api_key="fake-key", fetcher=lambda: fixture)

    assert len(headlines) == 1
    assert "ACME beats earnings" in headlines[0]["text"]


def test_fetch_news_headlines_records_fetch_failure_when_errors_list_given():
    # A genuine NewsAPI failure (401/429, a timeout, malformed JSON)
    # previously degraded to the SAME silent [] as "zero articles found" --
    # indistinguishable downstream, so a real outage could change a
    # candidate's classification with no explanation in scores.md (Codex
    # review on PR #403: "Preserve NewsAPI failures in scoring artifacts").
    from signals import fetch_news_headlines

    def broken_fetcher():
        raise ConnectionError("boom")

    errors: list[str] = []
    headlines = fetch_news_headlines("ACME", news_api_key="fake-key", fetcher=broken_fetcher, errors=errors)

    assert headlines == []
    assert any("ACME" in message and "boom" in message for message in errors)


def test_fetch_news_headlines_redacts_api_key_url_from_fetch_error():
    # requests.raise_for_status() embeds the full request URL -- including
    # NewsAPI's apiKey query parameter -- in its exception text. That
    # message flows into candidate rationale and gets written to
    # scores.json/scores.md; persisting it unmodified would materialize the
    # API key in repository-local artifacts that may get committed (Codex
    # review on PR #403: "Redact the NewsAPI key from persisted request
    # errors").
    from signals import fetch_news_headlines

    def broken_fetcher():
        raise ConnectionError("401 Client Error: Unauthorized for url: https://newsapi.org/v2/everything?apiKey=SECRET123")

    errors: list[str] = []
    fetch_news_headlines("ACME", news_api_key="fake-key", fetcher=broken_fetcher, errors=errors)

    assert not any("SECRET123" in message for message in errors)
    assert not any("https://" in message for message in errors)


def test_fetch_news_headlines_treats_null_articles_as_empty():
    # A successful NewsAPI response can carry "articles": null (not just an
    # absent key). Without normalizing that to [], the loop over `articles`
    # raises TypeError -- and since this fetch runs before the SEC fetch in
    # sentiment_score(), that would discard the whole sentiment signal
    # (including otherwise-available SEC filings) for the candidate (Codex
    # review on PR #403: "Treat null NewsAPI article lists as empty").
    from signals import fetch_news_headlines

    fixture = {"articles": None}

    headlines = fetch_news_headlines("ACME", news_api_key="fake-key", fetcher=lambda: fixture)

    assert headlines == []


def test_fetch_news_headlines_treats_non_list_articles_as_empty():
    # `or []` only substitutes when "articles" is falsy -- a truthy
    # non-list value (e.g. {"articles": 1}) sails through unchanged, and
    # iterating a non-iterable raises TypeError OUTSIDE the fetch exception
    # handler. Since this fetch runs before the SEC fetch in
    # sentiment_score(), that would discard otherwise-available SEC
    # sentiment too (Codex review on PR #403: "Validate the NewsAPI
    # articles collection").
    from signals import fetch_news_headlines

    fixture = {"articles": 1}

    headlines = fetch_news_headlines("ACME", news_api_key="fake-key", fetcher=lambda: fixture)

    assert headlines == []


def test_fetch_news_headlines_skips_non_string_article_fields():
    # The dict-shape check on each article only guards the article itself
    # -- a dict article whose "title"/"description" is some other JSON type
    # (a number, a nested object) previously still reached str.join(),
    # which raises TypeError on a non-str element, discarding the whole
    # sentiment signal instead of just skipping the malformed field (Codex
    # review on PR #403: "Validate NewsAPI article field types before
    # joining").
    from signals import fetch_news_headlines

    fixture = {
        "articles": [
            {"title": 12345, "description": {"nested": "object"}},
            {"title": "Real headline", "description": None},
        ]
    }

    headlines = fetch_news_headlines("ACME", news_api_key="fake-key", fetcher=lambda: fixture)

    assert headlines == [{"ticker": "ACME", "text": "Real headline"}]


def test_fetch_news_headlines_records_error_for_non_dict_payload():
    # A response that parses as valid JSON but isn't a mapping at the top
    # level (null, a bare list, ...) would otherwise raise AttributeError on
    # `.get("articles")`, OUTSIDE the fetch exception handler -- and since
    # this fetch runs before the SEC fetch in sentiment_score(), that
    # uncaught error would discard the whole sentiment signal (including
    # otherwise-available SEC filings) for the candidate (Codex review on
    # PR #403: "Validate the top-level NewsAPI payload").
    from signals import fetch_news_headlines

    errors: list[str] = []
    headlines = fetch_news_headlines("ACME", news_api_key="fake-key", fetcher=lambda: None, errors=errors)

    assert headlines == []
    assert any("ACME" in message for message in errors)


def test_fetch_news_headlines_sends_api_key_as_header_not_query_param():
    # The key belongs in a header, not a query string -- a URL query string
    # is copied into proxy/access logs and APM tooling far beyond this
    # process. The repo's existing NewsCollector
    # (trading-strategies/sentiment_monitor.py) sends it via X-Api-Key for
    # the same reason (Codex review on PR #403: "Keep the NewsAPI key out
    # of the request URL").
    import signals

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"articles": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResponse()

    fake_requests = types.SimpleNamespace(get=fake_get)
    with mock.patch.dict(sys.modules, {"requests": fake_requests}):
        signals.fetch_news_headlines("ACME", news_api_key="SECRET123")

    assert "apiKey" not in captured["params"]
    assert captured["headers"]["X-Api-Key"] == "SECRET123"


def test_fetch_news_headlines_anchors_date_range_to_as_of():
    # Same anchoring concern as fetch_sec_filings's as_of parameter --
    # the default fetcher otherwise recomputes "now" independently on
    # every call, so candidates scored before and after a run crosses UTC
    # midnight would get different windows purely from scoring order
    # (Antigravity review on PR #403: "Anchor sentiment lookbacks to the
    # run timestamp").
    from datetime import datetime, timezone

    import signals

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"articles": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _FakeResponse()

    fake_requests = types.SimpleNamespace(get=fake_get)
    with mock.patch.dict(sys.modules, {"requests": fake_requests}):
        signals.fetch_news_headlines(
            "ACME", news_api_key="SECRET123", days=7, as_of=datetime(2020, 6, 15, tzinfo=timezone.utc)
        )

    assert captured["params"]["from"] == "2020-06-08"


def test_fetch_news_headlines_bounds_results_at_as_of():
    # Without an upper bound, NewsAPI can return articles published AFTER
    # the captured `as_of` instant -- a long-lived run (a cold multi-GB
    # FinBERT init, a large candidate batch) could pick up genuinely-new
    # articles that appeared while it was running, making results depend
    # on candidate query order even with `from` anchored (Antigravity
    # review on PR #403: "Bound NewsAPI results at the run timestamp").
    from datetime import datetime, timezone

    import signals

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"articles": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _FakeResponse()

    fake_requests = types.SimpleNamespace(get=fake_get)
    run_instant = datetime(2020, 6, 15, 12, 30, tzinfo=timezone.utc)
    with mock.patch.dict(sys.modules, {"requests": fake_requests}):
        signals.fetch_news_headlines("ACME", news_api_key="SECRET123", days=7, as_of=run_instant)

    assert captured["params"]["to"] == run_instant.isoformat()


def test_sec_rate_limiter_enforces_minimum_spacing(monkeypatch):
    # A large candidate batch scored from local archives (fast, no network
    # wait per candidate) can issue EDGAR requests faster than SEC's
    # fair-access ceiling, causing later candidates in the batch to 403 and
    # lose their SEC sentiment based purely on input order (Codex review on
    # PR #403: "Throttle SEC requests across the candidate batch").
    import time

    from signals import _SecRateLimiter

    current = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: current["t"])

    def fake_sleep(seconds):
        current["t"] += seconds

    monkeypatch.setattr(time, "sleep", fake_sleep)

    limiter = _SecRateLimiter(max_per_second=2.0)  # 0.5s minimum spacing

    limiter.acquire()
    assert current["t"] == pytest.approx(0.0)

    limiter.acquire()
    assert current["t"] == pytest.approx(0.5)

    limiter.acquire()
    assert current["t"] == pytest.approx(1.0)


def test_sec_rate_limiter_acquire_holds_lock(monkeypatch):
    import time

    from signals import _SecRateLimiter

    entered = {"value": 0}

    class _FakeLock:
        def __enter__(self):
            entered["value"] += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    limiter = _SecRateLimiter(max_per_second=2.0)
    monkeypatch.setattr(limiter, "_lock", _FakeLock())
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    limiter.acquire()

    assert entered["value"] == 1


def test_fetch_sec_filings_default_fetcher_uses_shared_rate_limiter():
    # The rate limiter must actually be wired into the real HTTP fetcher
    # (not just exist as an unused class) -- verified here without a real
    # sleep by swapping in a fake limiter and checking it was invoked.
    import signals

    calls = {"acquire": 0}

    class _FakeLimiter:
        def acquire(self):
            calls["acquire"] += 1

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"hits": {"hits": []}}

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse()

    fake_requests = types.SimpleNamespace(get=fake_get)
    with mock.patch.object(signals, "_sec_rate_limiter", _FakeLimiter()):
        with mock.patch.dict(sys.modules, {"requests": fake_requests}):
            signals.fetch_sec_filings("ACME")

    assert calls["acquire"] == 1


def test_finbert_sentiment_analyzer_score_texts_holds_inference_lock(monkeypatch):
    import types

    import signals

    events = []

    class _FakeTensor:
        pass

    class _FakeTorch:
        class _NoGrad:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

        @staticmethod
        def no_grad():
            return _FakeTorch._NoGrad()

        class nn:
            class functional:
                @staticmethod
                def softmax(_logits, dim=-1):
                    return types.SimpleNamespace(tolist=lambda: [[0.1, 0.2, 0.7]])

    class _FakeConfig:
        id2label = {0: "negative", 1: "neutral", 2: "positive"}

    class _FakeModel:
        config = _FakeConfig()

        def eval(self):
            return self

        def __call__(self, **_inputs):
            events.append("model_call")
            return types.SimpleNamespace(logits=_FakeTensor())

    class _FakeLock:
        def __enter__(self):
            events.append("lock_enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("lock_exit")
            return False

    def fake_tokenizer(_chunk, **_kwargs):
        events.append("tokenizer_call")
        return {"input_ids": _FakeTensor()}

    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda *args, **kwargs: fake_tokenizer),
        AutoModelForSequenceClassification=types.SimpleNamespace(from_pretrained=lambda *args, **kwargs: _FakeModel()),
    )

    with mock.patch.dict(sys.modules, {"transformers": fake_transformers, "torch": _FakeTorch()}):
        analyzer = signals.FinbertSentimentAnalyzer(revision="abc123")
        monkeypatch.setattr(analyzer, "_inference_lock", _FakeLock())
        scored = analyzer.score_texts(["x"])

    assert scored[0]["label"] == "positive"
    assert events == ["lock_enter", "tokenizer_call", "model_call", "lock_exit"]
