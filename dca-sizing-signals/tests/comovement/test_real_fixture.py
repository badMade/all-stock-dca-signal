"""One check against REAL prices (project rule: real verified fixtures, not invented ones).

fixtures/rh_daily_closes_6_tickers.csv holds split-adjusted Robinhood daily closes for
STX, WDC, KR, TSM, QBTS and RGTI, 2025-10-17 to 2026-10-30 (provenance in its header).
Only facts that the 2026-11-02 co-movement research also found over the full two years
are asserted here, and only with wide margins, so the test checks the pipeline, not one
particular window:

  * STX/WDC (storage) is the tightest genuine single-stock pair (0.87 over two years);
  * QBTS/RGTI (quantum) move together (0.78 over two years);
  * KR is negatively correlated with the semis (KR/TSM -0.36 over two years) and never
    joins their cluster.

Measured on this trimmed window when the fixture was cut: STX/WDC 0.88, QBTS/RGTI 0.93,
KR/TSM -0.41; clusters {KR}, {QBTS, RGTI}, {STX, TSM, WDC}.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from comovement_signals import build_clusters, pairwise_correlations, returns_matrix

FIXTURE = Path(__file__).parent / "fixtures" / "rh_daily_closes_6_tickers.csv"
AS_OF = date(2026, 10, 31)  # the day after the last close, so every bar is final by date


def _rows():
    with FIXTURE.open() as f:
        lines = [line for line in f if not line.startswith("#")]
    out = []
    for r in csv.DictReader(lines):
        assert r["interpolated"] in ("True", "False")
        out.append({"ticker": r["ticker"], "date": r["date"], "close": float(r["close"]),
                    "interpolated": r["interpolated"] == "True"})
    return out


def _cluster_of(clusters, ticker):
    return next(c for c in clusters if ticker in c)


def test_fixture_shape():
    rows = _rows()
    assert {r["ticker"] for r in rows} == {"STX", "WDC", "KR", "TSM", "QBTS", "RGTI"}
    assert len(rows) == 6 * 261


def test_real_prices_cluster_as_the_research_found():
    m = returns_matrix(_rows(), AS_OF)  # production defaults: 252 sessions, 120 minimum
    assert len(m.sessions) == 252
    assert m.singletons == ()
    corr = pairwise_correlations(m.returns, m.eligible)
    assert corr[("STX", "WDC")] > 0.75
    assert corr[("QBTS", "RGTI")] > 0.65
    assert corr[("KR", "TSM")] < 0.0

    clusters = build_clusters(m)  # production cut 0.5
    assert {"STX", "WDC"} <= set(_cluster_of(clusters, "STX"))
    assert {"QBTS", "RGTI"} <= set(_cluster_of(clusters, "QBTS"))
    kr = _cluster_of(clusters, "KR")
    assert "TSM" not in kr and "STX" not in kr and "WDC" not in kr
