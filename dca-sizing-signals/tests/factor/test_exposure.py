"""F2 (docs/adr-factor-residual-dip-signals.md): the read-only factor exposure report."""

from __future__ import annotations

import json
import math
import random

import pytest

from factor_signals import (
    EXPOSURE_REPORT_FILENAME,
    FactorInputError,
    SectorMap,
    exposure_report,
    fit_factor_model,
    write_exposure_report,
)

from ._synth import as_of_after, closes_from_returns, market_and_sector_returns

N = 300


def _model():
    m, s, _ = market_and_sector_returns(N)
    rng = random.Random(11)
    aaa = [0.9 * mi + 0.6 * si + rng.gauss(0, 0.01) for mi, si in zip(m, s, strict=True)]
    bbb = [0.5 * mi + 0.2 * si + rng.gauss(0, 0.02) for mi, si in zip(m, s, strict=True)]
    qqq = [1.1 * mi + rng.gauss(0, 0.004) for mi in m]
    rows = (closes_from_returns("SPY", m) + closes_from_returns("XLK", s) + closes_from_returns("AAA", aaa)
            + closes_from_returns("BBB", bbb) + closes_from_returns("QQQ", qqq))
    smap = SectorMap(version="test-v1", etf_by_ticker={"AAA": "XLK", "BBB": "XLK", "QQQ": None})
    return fit_factor_model(rows, as_of_after(rows), ["AAA", "BBB", "QQQ", "ZZZ"], smap)


def test_book_aggregates_are_dollar_weighted() -> None:
    model = _model()
    report = exposure_report(model, {"holdings": {"AAA": 300.0, "BBB": 100.0, "QQQ": 100.0, "ZZZ": 50.0}})
    book = report["books"]["holdings"]
    t = model.tickers
    w = {"AAA": 0.6, "BBB": 0.2, "QQQ": 0.2}
    assert book["total_usd"] == pytest.approx(550.0)
    assert book["covered_usd"] == pytest.approx(500.0)
    assert book["uncovered"] == [{"ticker": "ZZZ", "usd": 50.0, "reason": "unmapped"}]
    assert book["portfolio_beta_market"] == pytest.approx(sum(w[k] * t[k].beta_market for k in w))
    assert book["sector_weights"] == pytest.approx({"XLK": 0.8, "market_only": 0.2})
    # beta_sector is Optional (None for a market-only ticker like QQQ); AAA and BBB are
    # XLK-mapped, so narrow them explicitly -- a None should fail here, not TypeError.
    aaa_sector, bbb_sector = t["AAA"].beta_sector, t["BBB"].beta_sector
    assert aaa_sector is not None and bbb_sector is not None
    assert book["sector_loadings"] == pytest.approx({"XLK": 0.6 * aaa_sector + 0.2 * bbb_sector})
    assert book["mean_r_squared"] == pytest.approx(sum(w[k] * t[k].r_squared for k in w))
    assert book["effective_names"] == pytest.approx(1.0 / (0.36 + 0.04 + 0.04))


def test_variance_decomposition_matches_factor_model() -> None:
    model = _model()
    book = exposure_report(model, {"h": {"AAA": 1.0, "QQQ": 1.0}})["books"]["h"]
    t = model.tickers
    x_m = 0.5 * t["AAA"].beta_market + 0.5 * t["QQQ"].beta_market
    # beta_sector is Optional (None for market-only QQQ); AAA is XLK-mapped, so narrow it.
    aaa_sector = t["AAA"].beta_sector
    assert aaa_sector is not None
    x_s = 0.5 * aaa_sector
    f = model.factor_returns
    common = sorted(set(f["SPY"]) & set(f["XLK"]))

    def cov(a, b):
        abar = sum(a[d] for d in common) / len(common)
        bbar = sum(b[d] for d in common) / len(common)
        return sum((a[d] - abar) * (b[d] - bbar) for d in common) / (len(common) - 1)

    factor_var = x_m**2 * cov(f["SPY"], f["SPY"]) + x_s**2 * cov(f["XLK"], f["XLK"]) + 2 * x_m * x_s * cov(
        f["SPY"], f["XLK"])
    idio_var = 0.25 * t["AAA"].sigma_idio_daily**2 + 0.25 * t["QQQ"].sigma_idio_daily**2
    assert book["daily_vol"] == pytest.approx(math.sqrt(factor_var + idio_var), rel=1e-9)
    assert book["factor_variance_share"] == pytest.approx(factor_var / (factor_var + idio_var), rel=1e-9)


def test_hypothetical_all_escalate_book_is_reported_beside_holdings() -> None:
    """The report takes any number of named books; the caller computes the 'every eligible
    slot hits 2x/3x' spend (this package cannot import the ladder)."""
    report = exposure_report(_model(), {"holdings": {"AAA": 10.0}, "all_escalate": {"AAA": 30.0, "BBB": 30.0}})
    assert set(report["books"]) == {"holdings", "all_escalate"}
    assert report["books"]["all_escalate"]["covered_usd"] == pytest.approx(60.0)


def test_empty_or_uncovered_book_has_null_aggregates() -> None:
    book = exposure_report(_model(), {"h": {"ZZZ": 5.0}})["books"]["h"]
    assert book["covered_usd"] == 0.0
    for key in ("portfolio_beta_market", "mean_r_squared", "effective_names", "daily_vol",
                "factor_variance_share"):
        assert book[key] is None


@pytest.mark.parametrize("usd", [-1.0, float("nan"), float("inf"), True, "10"])
def test_bad_dollar_amount_raises(usd) -> None:
    with pytest.raises(FactorInputError):
        exposure_report(_model(), {"h": {"AAA": usd}})


def test_report_metadata_and_json_safety(tmp_path) -> None:
    model = _model()
    report = exposure_report(model, {"h": {"AAA": 1.0}})
    assert report["schema"] == "factor_exposure_report/v1"
    assert report["as_of"] == model.as_of.isoformat()
    assert report["sector_map_version"] == "test-v1"
    assert report["skipped"] == {"ZZZ": "unmapped"}
    assert report["tickers"]["AAA"]["sector_etf"] == "XLK"
    path = write_exposure_report(tmp_path / EXPOSURE_REPORT_FILENAME, report)
    assert json.loads(path.read_text()) == report


def test_writer_refuses_other_names_and_older_as_of(tmp_path) -> None:
    report = exposure_report(_model(), {"h": {"AAA": 1.0}})
    with pytest.raises(ValueError):
        write_exposure_report(tmp_path / "ml_sizing_adjustment.json", report)
    path = tmp_path / EXPOSURE_REPORT_FILENAME
    newer = dict(report, as_of="2099-01-01")
    write_exposure_report(path, newer)
    with pytest.raises(ValueError, match="older"):
        write_exposure_report(path, report)
    assert json.loads(path.read_text())["as_of"] == "2099-01-01"
