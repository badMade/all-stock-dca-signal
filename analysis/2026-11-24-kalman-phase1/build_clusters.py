"""Research one-off: point-in-time co-movement clusters for the Kalman Phase 1 backtest.

Clusters are built with comovement_signals.build_clusters on returns up to AS_OF (the
session before the backtest window), so membership uses no data from the scored period.
Output: {ticker: [members of its cluster]} for every ticker in a cluster of size >= 2.
Run from dca-sizing-signals/: python <this> <history-dir> <out.json>
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from comovement_signals.clustering import build_clusters
from comovement_signals.returns import returns_matrix
from kalman_signals.backtest_cli import DEFAULT_UNIVERSE, load_csv

AS_OF = date(2023, 12, 29)
NON_EQUITY = {"AAAU", "XLG", "QQQ", "VT", "TOPT", "QTOP", "FLKR", "DXYZ"}

history, out = Path(sys.argv[1]), Path(sys.argv[2])
universe = [t if isinstance(t, str) else t["symbol"]
            for t in json.loads(DEFAULT_UNIVERSE.read_text())["active"]]
rows = []
for t in universe:
    if t in NON_EQUITY or not (history / f"{t}.csv").is_file():
        continue
    loaded, _ = load_csv(history / f"{t}.csv", t, AS_OF - timedelta(days=500), AS_OF)
    rows += [{"ticker": r["ticker"], "date": r["date"], "close": r["close"], "final": True} for r in loaded]
matrix = returns_matrix(rows, AS_OF)
clusters = build_clusters(matrix)
members = {t: list(c) for c in clusters if len(c) >= 2 for t in c}
out.write_text(json.dumps({"as_of": AS_OF.isoformat(), "clusters": [list(c) for c in clusters if len(c) >= 2],
                           "members": members}, indent=2) + "\n")
sizes = sorted((len(c) for c in clusters if len(c) >= 2), reverse=True)
print(f"eligible={len(matrix.eligible)} singletons_short_history={len(matrix.singletons)} "
      f"multi_clusters={len(sizes)} sizes={sizes} clustered_tickers={len(members)}")
