"""F2: the read-only factor exposure report.

Spec: docs/adr-factor-residual-dip-signals.md, component F2. A report, not a suggester: it
never publishes a multiplier and nothing in the live path reads it.

For each caller-named "book" (a ticker -> dollars mapping) it reports, over the tickers F1
could fit:

  * portfolio_beta_market: dollar-weighted shrunk market beta;
  * sector_weights: dollar share by sector ETF (``market_only`` for tickers mapped to None);
  * sector_loadings: dollar-weighted shrunk loading on each orthogonalized sector factor;
  * mean_r_squared: dollar-weighted share of daily variance the factors explain;
  * effective_names: 1 / sum(w^2), the dollar-Herfindahl count of independent positions
    (it ignores correlation, which the next two fields cover);
  * daily_vol and factor_variance_share: from the factor model
    var = x' F x + sum(w_i^2 sigma_i^2), where x is the book's factor exposure vector and
    F the sample covariance of the factor series over the window.

The ADR's report also suggested a PCA "effective number of bets"; stdlib has no eigen
solver, so factor_variance_share stands in for it here (registered as a later trial if
wanted).

The books are the caller's: typically current holdings, and the hypothetical spend if every
eligible slot hit 2x/3x tomorrow. This package cannot import the ladder
(tests/test_architecture_boundaries.py), so the caller computes that spend.

A ticker in a book that F1 skipped is listed under ``uncovered`` with F1's reason and left
out of every aggregate. Its dollars still count in ``total_usd`` so the gap is visible.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from factor_signals.betas import SKIP_UNMAPPED, FactorInputError, FactorModel

SCHEMA = "factor_exposure_report/v1"
EXPOSURE_REPORT_FILENAME = "factor_exposure_report.json"
MARKET_ONLY = "market_only"


def _check_book(name: str, book: Mapping[str, object]) -> dict[str, float]:
    if not isinstance(name, str) or not name:
        raise FactorInputError(f"book name must be a non-empty string: {name!r}")
    out: dict[str, float] = {}
    for ticker, usd in book.items():
        if not isinstance(ticker, str) or not ticker:
            raise FactorInputError(f"book {name}: ticker must be a non-empty string: {ticker!r}")
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd < 0:
            raise FactorInputError(f"book {name}: dollars for {ticker} must be finite and >= 0: {usd!r}")
        out[ticker] = float(usd)
    return out


def _covariance(a: Mapping[date, float], b: Mapping[date, float]) -> float:
    """Sample covariance over the dates both series share (0.0 below two shared dates)."""
    common = [d for d in a if d in b]
    if len(common) < 2:
        return 0.0
    abar = sum(a[d] for d in common) / len(common)
    bbar = sum(b[d] for d in common) / len(common)
    return sum((a[d] - abar) * (b[d] - bbar) for d in common) / (len(common) - 1)


def _book_report(model: FactorModel, book: Mapping[str, float]) -> dict[str, object]:
    total = sum(book.values())
    covered = {t: usd for t, usd in book.items() if t in model.tickers and usd > 0}
    uncovered = [
        {"ticker": t, "usd": usd, "reason": model.skipped.get(t, SKIP_UNMAPPED)}
        for t, usd in sorted(book.items())
        if t not in model.tickers and usd > 0
    ]
    covered_usd = sum(covered.values())
    report: dict[str, object] = {
        "total_usd": total,
        "covered_usd": covered_usd,
        "uncovered": uncovered,
        "portfolio_beta_market": None,
        "sector_weights": {},
        "sector_loadings": {},
        "mean_r_squared": None,
        "effective_names": None,
        "daily_vol": None,
        "factor_variance_share": None,
    }
    if covered_usd <= 0:
        return report

    w = {t: usd / covered_usd for t, usd in covered.items()}
    betas = model.tickers
    sector_weights: dict[str, float] = {}
    # Factor exposure vector x, keyed by factor symbol (the market, then each sector ETF).
    x: dict[str, float] = {model.market: 0.0}
    for t, wt in w.items():
        b = betas[t]
        x[model.market] += wt * b.beta_market
        key = b.sector_etf if b.sector_etf is not None else MARKET_ONLY
        sector_weights[key] = sector_weights.get(key, 0.0) + wt
        if b.sector_etf is not None and b.beta_sector is not None:
            x[b.sector_etf] = x.get(b.sector_etf, 0.0) + wt * b.beta_sector

    factors = sorted(x)
    factor_var = sum(
        x[f] * x[g] * _covariance(model.factor_returns[f], model.factor_returns[g])
        for f in factors
        for g in factors
    )
    idio_var = sum((wt * betas[t].sigma_idio_daily) ** 2 for t, wt in w.items())
    total_var = factor_var + idio_var

    report.update(
        portfolio_beta_market=x[model.market],
        sector_weights=dict(sorted(sector_weights.items())),
        sector_loadings={f: x[f] for f in factors if f != model.market},
        mean_r_squared=sum(wt * betas[t].r_squared for t, wt in w.items()),
        effective_names=1.0 / sum(wt * wt for wt in w.values()),
    )
    # Pairwise-sample covariances need not form a PSD matrix; a non-positive total is
    # reported as unknown rather than as a negative variance.
    if total_var > 0 and factor_var >= 0:
        report["daily_vol"] = math.sqrt(total_var)
        report["factor_variance_share"] = factor_var / total_var
    return report


def exposure_report(
    model: FactorModel,
    books: Mapping[str, Mapping[str, object]],
    generated_at: str | None = None,
) -> dict[str, object]:
    """Build the F2 report as a JSON-safe dict.

    Args:
        model: an F1 FactorModel.
        books: book name -> {ticker: dollars >= 0}.
        generated_at: optional ISO timestamp supplied by the caller (no clock here).

    Raises:
        FactorInputError: on a malformed book.
    """
    checked = {name: _check_book(name, book) for name, book in books.items()}
    return {
        "schema": SCHEMA,
        "as_of": model.as_of.isoformat(),
        "generated_at": generated_at,
        "model_version": model.model_version,
        "sector_map_version": model.sector_map_version,
        "market": model.market,
        "latest_session": model.latest_session.isoformat(),
        "window_sessions": model.window_sessions,
        "sector_fits": {
            etf: {"alpha": f.alpha, "beta_on_market": f.beta_on_market, "n_obs": f.n_obs}
            for etf, f in sorted(model.sector_fits.items())
        },
        "tickers": {
            t: {
                "sector_etf": b.sector_etf,
                "n_obs": b.n_obs,
                "beta_market": b.beta_market,
                "beta_sector": b.beta_sector,
                "beta_market_raw": b.beta_market_raw,
                "beta_sector_raw": b.beta_sector_raw,
                "sigma_idio_daily": b.sigma_idio_daily,
                "r_squared": b.r_squared,
            }
            for t, b in sorted(model.tickers.items())
        },
        "skipped": dict(sorted(model.skipped.items())),
        "books": {name: _book_report(model, book) for name, book in sorted(checked.items())},
    }


def write_exposure_report(path: Path, report: Mapping[str, object]) -> Path:
    """Atomically write the report (tmp file, then os.replace).

    Refuses any file name but EXPOSURE_REPORT_FILENAME, so it can never overwrite a
    suggestion file, and refuses to replace a report with a later ``as_of`` (an older run
    finishing late never overwrites a newer one). The directory is always the caller's.
    """
    path = Path(path)
    if path.name != EXPOSURE_REPORT_FILENAME:
        raise ValueError(f"the exposure report is written only as {EXPOSURE_REPORT_FILENAME}, not {path.name}")
    new_as_of = date.fromisoformat(str(report["as_of"]))
    if path.exists():
        try:
            existing = date.fromisoformat(str(json.loads(path.read_text())["as_of"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"existing {path.name} is unreadable; refusing to overwrite it") from exc
        if existing > new_as_of:
            raise ValueError(f"refusing to replace a report for {existing} with an older one for {new_as_of}")
    # allow_nan=False: a NaN or inf in the report is a bug, not something to write down.
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return path
