"""Research one-off: turn the Phase 1 run reports into LedgerStore.ingest_trials rows.

  python record_trials.py rows  <out.json> [<reports-dir>]  # build rows only (no ledger touched)
  python record_trials.py ingest <rows.json> <ledger.duckdb> # write them; run only when approved

Each walk-forward run is one trial. Its q grid (3 values, chosen per model by predictive
likelihood) is recorded in config, so the count of Q settings tried is visible too.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(sys.argv[3] if sys.argv[1] == "rows" and len(sys.argv) > 3 else ".").resolve()
RUNS = {
    "r1_vt": {"market": "VT", "clusters": None, "earnings": None},
    "r2_spy": {"market": "SPY", "clusters": None, "earnings": None},
    "r3_spy_clusters": {"market": "SPY", "clusters": "point_in_time_2023-12-29", "earnings": None},
    "r4_spy_clusters_earnings": {"market": "SPY", "clusters": "point_in_time_2023-12-29",
                                 "earnings": "yfinance_announcements_pad_1"},
}


def rows() -> list[dict]:
    out = []
    for name, extra in RUNS.items():
        r = json.loads((HERE / f"{name}.json").read_text())
        cal, reg, b = r["calibration"], r["calibration_by_regime"], r["forward_adjusted_by_intraday_z"]
        metrics = {
            "variance": cal["variance"], "share_beyond_1_96": cal["share_beyond_1_96"],
            "variance_high_vol": reg["high_vol"]["variance"], "variance_low_vol": reg["low_vol"]["variance"],
            "gate_passed": float(r["gate_passed"]), "gate_checks_failed": float(sum(not v for v in r["gate"].values())),
            **{f"fwd5_mean[{k}]": v["mean"] for k, v in b.items() if v["mean"] is not None},
            **{f"fwd5_se[{k}]": v["se_date_clustered"] for k, v in b.items() if v["se_date_clustered"] is not None},
        }
        out.append({
            "trial_id": f"kalman_residual_dip/phase1/{name}",
            "experiment": "kalman_residual_dip_phase1",
            "config": {"q_grid": r["trials"]["q_grid"], "window_sessions": 504, "burn_in": 60, "horizon": 5,
                       "start": r["first_session"], "end": r["last_session"], "refit": "monthly", **extra},
            "metrics": metrics,
            "n_observations": r["events"],
            "evaluated_at": "2026-11-24T00:00:00+00:00",
        })
    return out


if sys.argv[1] == "rows":
    Path(sys.argv[2]).write_text(json.dumps(rows(), indent=1) + "\n")
    print(f"{len(rows())} trial rows -> {sys.argv[2]}")
elif sys.argv[1] == "ingest":
    from market_data_ingest.ledger.store import LedgerStore
    data = [{**r, "evaluated_at": datetime.fromisoformat(r["evaluated_at"])} for r in json.loads(Path(sys.argv[2]).read_text())]
    store = LedgerStore(sys.argv[3])
    n = store.ingest_trials(data, source="kalman_signals.backtest_cli", run_id="kalman-phase1-2026-11-24",
                            as_of=datetime.now(timezone.utc))
    print(f"inserted {n}")
