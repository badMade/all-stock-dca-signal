"""F1 (docs/adr-factor-residual-dip-signals.md): point-in-time, Blume-shrunk market and
orthogonalized-sector betas. Tests 1 to 3 of the ADR's test list, for F1."""

from __future__ import annotations

import math
import random
from datetime import timedelta

import pytest

from factor_signals import (
    BLUME_INTERCEPT,
    BLUME_SLOPE,
    FactorInputError,
    SectorMap,
    fit_factor_model,
)

from ._synth import as_of_after, closes_from_returns, day, market_and_sector_returns

N = 300  # returns generated; the default window keeps the newest 252
WINDOW = 252


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting, written here so the reference fit
    shares no code with the module under test."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _ols(y: list[float], xs: list[list[float]]) -> tuple[list[float], float, float]:
    """Reference OLS with intercept: (coefs incl. intercept first, sigma, r_squared)."""
    cols = [[1.0] * len(y)] + xs
    k = len(cols)
    xtx = [[sum(cols[i][t] * cols[j][t] for t in range(len(y))) for j in range(k)] for i in range(k)]
    xty = [sum(cols[i][t] * y[t] for t in range(len(y))) for i in range(k)]
    beta = _solve(xtx, xty)
    resid = [y[t] - sum(beta[i] * cols[i][t] for i in range(k)) for t in range(len(y))]
    sse = sum(e * e for e in resid)
    ybar = sum(y) / len(y)
    sst = sum((v - ybar) ** 2 for v in y)
    return beta, math.sqrt(sse / (len(y) - k)), 1.0 - sse / sst


def _universe(ticker_returns: dict[str, list[float]], seed: int = 7):
    m, s, _ = market_and_sector_returns(N, seed)
    rows = closes_from_returns("SPY", m) + closes_from_returns("XLK", s)
    for t, r in ticker_returns.items():
        rows += closes_from_returns(t, r)
    return m, s, rows


def _map(**extra) -> SectorMap:
    return SectorMap(version="test-v1", etf_by_ticker={"AAA": "XLK", "BBB": "XLK", **extra})


def test_exact_two_factor_loading_recovered() -> None:
    m, s, _ = market_and_sector_returns(N)
    aaa = [0.0002 + 0.9 * mi + 0.7 * si for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": aaa})
    model = fit_factor_model(rows, as_of_after(rows), ["AAA"], _map())

    fit = model.sector_fits["XLK"]
    ref, _, _ = _ols(s[-WINDOW:], [m[-WINDOW:]])
    assert fit.beta_on_market == pytest.approx(ref[1], rel=1e-9)
    assert fit.alpha == pytest.approx(ref[0], abs=1e-12)
    assert fit.n_obs == WINDOW

    b = model.tickers["AAA"]
    # ticker = c + 0.9 m + 0.7 s = (c + 0.7 a) + (0.9 + 0.7 b) m + 0.7 e_s exactly.
    assert b.beta_sector_raw == pytest.approx(0.7, rel=1e-8)
    assert b.beta_market_raw == pytest.approx(0.9 + 0.7 * fit.beta_on_market, rel=1e-8)
    assert b.r_squared == pytest.approx(1.0, abs=1e-9)
    assert b.sigma_idio_daily == pytest.approx(0.0, abs=1e-9)
    assert b.n_obs == WINDOW


def test_noisy_fit_matches_independent_reference_ols() -> None:
    m, s, _ = market_and_sector_returns(N)
    rng = random.Random(99)
    aaa = [0.9 * mi + 0.4 * si + rng.gauss(0, 0.012) for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": aaa})
    model = fit_factor_model(rows, as_of_after(rows), ["AAA"], _map())

    # Regressing on (m, e_s) spans the same space as (m, s), so the coefficients map exactly.
    ref, sigma, r2 = _ols(aaa[-WINDOW:], [m[-WINDOW:], s[-WINDOW:]])
    b_hat = model.sector_fits["XLK"].beta_on_market
    b = model.tickers["AAA"]
    assert b.beta_sector_raw == pytest.approx(ref[2], rel=1e-8)
    assert b.beta_market_raw == pytest.approx(ref[1] + ref[2] * b_hat, rel=1e-8)
    assert b.sigma_idio_daily == pytest.approx(sigma, rel=1e-8)
    assert b.r_squared == pytest.approx(r2, rel=1e-8)


def test_blume_shrinkage_applied_to_both_betas() -> None:
    m, s, _ = market_and_sector_returns(N)
    rng = random.Random(3)
    aaa = [1.6 * mi + 0.3 * si + rng.gauss(0, 0.01) for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": aaa})
    b = fit_factor_model(rows, as_of_after(rows), ["AAA"], _map()).tickers["AAA"]
    assert (BLUME_INTERCEPT, BLUME_SLOPE) == (0.23, 0.77)
    assert b.beta_market == pytest.approx(0.23 + 0.77 * b.beta_market_raw)
    # beta_sector_raw is Optional (None for a market-only ticker); AAA is sector-mapped,
    # so narrow it explicitly -- a None here should fail as an assertion, not a TypeError.
    assert b.beta_sector_raw is not None
    assert b.beta_sector == pytest.approx(0.23 + 0.77 * b.beta_sector_raw)


def test_market_only_ticker_by_design() -> None:
    """A sector map value of None means 'market only' (a broad ETF), not 'unmapped'."""
    m, _, _ = market_and_sector_returns(N)
    rng = random.Random(5)
    qqq = [1.1 * mi + rng.gauss(0, 0.004) for mi in m]
    _, _, rows = _universe({"QQQ": qqq})
    model = fit_factor_model(rows, as_of_after(rows), ["QQQ"], _map(QQQ=None))
    ref, sigma, r2 = _ols(qqq[-WINDOW:], [m[-WINDOW:]])
    b = model.tickers["QQQ"]
    assert b.sector_etf is None and b.beta_sector is None and b.beta_sector_raw is None
    assert b.beta_market_raw == pytest.approx(ref[1], rel=1e-9)
    assert b.sigma_idio_daily == pytest.approx(sigma, rel=1e-9)
    assert b.r_squared == pytest.approx(r2, rel=1e-9)


def test_never_uses_future_nonfinal_or_interpolated_rows() -> None:
    """ADR test 1: rows after as_of, a non-final bar on as_of, and interpolated rows are
    ignored, so adding wild ones changes nothing."""
    m, s, _ = market_and_sector_returns(N)
    aaa = [0.8 * mi + 0.5 * si for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": aaa})
    as_of = as_of_after(rows)
    clean = fit_factor_model(rows, as_of, ["AAA"], _map())

    noisy_rows = list(rows)
    for t in ("SPY", "XLK", "AAA"):
        noisy_rows.append({"ticker": t, "date": as_of, "close": 1.0})  # forming bar, not final
        noisy_rows.append({"ticker": t, "date": as_of + timedelta(days=1), "close": 9999.0, "final": True})
    noisy_rows.append({"ticker": "AAA", "date": day(-3), "close": None, "interpolated": True})
    noisy = fit_factor_model(noisy_rows, as_of, ["AAA"], _map())
    assert noisy.tickers == clean.tickers
    assert noisy.latest_session == clean.latest_session == as_of - timedelta(days=1)


def test_final_bar_on_as_of_is_used() -> None:
    m, s, _ = market_and_sector_returns(N)
    aaa = [0.8 * mi + 0.5 * si for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": aaa})
    last = max(r["date"] for r in rows)
    for r in rows:
        if r["date"] == last:
            r["final"] = True
    model = fit_factor_model(rows, last, ["AAA"], _map())
    assert model.latest_session == last


def test_only_trailing_window_is_used() -> None:
    m, s, _ = market_and_sector_returns(N)
    aaa = [0.8 * mi + 0.5 * si for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": aaa})
    as_of = as_of_after(rows)
    base = fit_factor_model(rows, as_of, ["AAA"], _map())
    # Wreck AAA's closes well before the window: the fit must not move.
    early = {day(i) for i in range(1, 20)}
    tampered = [dict(r, close=r["close"] * (5.0 if r["date"] in early and r["ticker"] == "AAA" else 1.0))
                for r in rows]
    assert fit_factor_model(tampered, as_of, ["AAA"], _map()).tickers == base.tickers


@pytest.mark.parametrize(
    "bad",
    [
        {"ticker": "AAA", "date": "2025-01-05"},  # no close
        {"ticker": "", "date": "2025-01-05", "close": 1.0},
        {"ticker": "AAA", "date": "not-a-date", "close": 1.0},
        {"ticker": "AAA", "date": "2025-01-05", "close": -1.0},
        {"ticker": "AAA", "date": "2025-01-05", "close": True},
        {"ticker": "AAA", "date": "2025-01-05", "close": 1.0, "final": "yes"},
    ],
)
def test_malformed_row_raises(bad: dict) -> None:
    """Fail closed: a malformed row is an error, never silently skipped."""
    _, _, rows = _universe({})
    with pytest.raises(FactorInputError):
        fit_factor_model(rows + [bad], as_of_after(rows), [], _map())


def test_duplicate_row_raises() -> None:
    _, _, rows = _universe({})
    with pytest.raises(FactorInputError, match="duplicate"):
        fit_factor_model(rows + [dict(rows[5])], as_of_after(rows), [], _map())


def test_stale_or_missing_market_fails_the_whole_run() -> None:
    """ADR test 3 for F1: without a fresh market series no ticker can be fit."""
    _, _, rows = _universe({})
    with pytest.raises(FactorInputError, match="stale"):
        fit_factor_model(rows, as_of_after(rows) + timedelta(days=10), [], _map())
    no_spy = [r for r in rows if r["ticker"] != "SPY"]
    with pytest.raises(FactorInputError, match="SPY"):
        fit_factor_model(no_spy, as_of_after(rows), [], _map())


def test_per_ticker_problems_are_skipped_with_reasons() -> None:
    m, s, _ = market_and_sector_returns(N)
    good = [0.8 * mi + 0.5 * si for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": good, "BBB": good, "NEW": good[-50:], "CCC": good})
    # BBB stops trading 5 sessions before the latest session: stale.
    cutoff = max(r["date"] for r in rows) - timedelta(days=5)
    rows = [r for r in rows if not (r["ticker"] == "BBB" and r["date"] > cutoff)]
    # NEW only has its last 50 returns (placed at the end of the calendar).
    rows = [r for r in rows if r["ticker"] != "NEW"] + closes_from_returns("NEW", good[-50:], first_day=N - 50)
    smap = _map(NEW="XLK", CCC="XLE", DDD="XLK")  # XLE has no prices; DDD has no prices
    model = fit_factor_model(rows, as_of_after(rows), ["AAA", "BBB", "NEW", "CCC", "DDD", "ZZZ"], smap)
    assert set(model.tickers) == {"AAA"}
    assert model.skipped == {
        "BBB": "stale",
        "NEW": "insufficient_history",
        "CCC": "sector_unavailable",
        "DDD": "no_prices",
        "ZZZ": "unmapped",
    }


def test_sector_map_validation() -> None:
    with pytest.raises(FactorInputError):
        SectorMap(version="", etf_by_ticker={})
    with pytest.raises(FactorInputError):
        SectorMap(version="v1", etf_by_ticker={"AAA": ""})
    with pytest.raises(FactorInputError):
        SectorMap(version="v1", etf_by_ticker={"": "XLK"})


def test_model_carries_versions_and_orthogonal_factor_returns() -> None:
    m, s, _ = market_and_sector_returns(N)
    aaa = [0.8 * mi + 0.5 * si for mi, si in zip(m, s, strict=True)]
    _, _, rows = _universe({"AAA": aaa})
    model = fit_factor_model(rows, as_of_after(rows), ["AAA"], _map())
    assert model.sector_map_version == "test-v1"
    assert model.model_version.startswith("factor_betas/")
    assert model.market == "SPY"
    assert len(model.sessions) == WINDOW
    e = model.factor_returns["XLK"]
    mk = model.factor_returns["SPY"]
    common = sorted(set(e) & set(mk))
    # The orthogonalized sector series is uncorrelated with the market on its fit sample.
    mbar = sum(mk[d] for d in common) / len(common)
    assert abs(sum(e[d] * (mk[d] - mbar) for d in common)) < 1e-12
    assert abs(sum(e[d] for d in common)) < 1e-12
