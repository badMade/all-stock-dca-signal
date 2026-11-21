"""Reads a walk-forward report.json and prints METRIC lines for the autoresearch loop.

Primary metric `mae_ratio`: mean over the evaluated horizons of octopus close MAE / persistence
close MAE at the SAME horizon (lower is better; <1 beats persistence). Secondary: pinball ratio,
worst-case coverage error vs the 80% target, and per-horizon-bucket ratios (short 1-10, mid 15-45, long 60-90).
"""
import json
import sys

# Persistence is deterministic and independent of the octopus knobs, so it is run once (autoresearch.sh
# caches it) and read from its own report; `--model octopus` reports contain no persistence column.
o = json.load(open(sys.argv[1]))["horizons"]["by_horizon"]["octopus"]
p = json.load(open(sys.argv[2]))["horizons"]["by_horizon"]["persistence"]
hs = sorted(int(h) for h in o if h in p)
mae = {h: o[str(h)]["close"]["mae"] / p[str(h)]["close"]["mae"] for h in hs}
pb = lambda r: sum(r["pinball"].values()) / len(r["pinball"])  # mean over p10/p50/p90
pin = {h: pb(o[str(h)]["close"]) / pb(p[str(h)]["close"]) for h in hs}
cov = {h: abs(o[str(h)]["close"]["coverage"] - 0.8) for h in hs}
mean = lambda xs: sum(xs) / len(xs)
print(f"METRIC mae_ratio={mean(mae.values()):.6f}")
print(f"METRIC pinball_ratio={mean(pin.values()):.6f}")
print(f"METRIC max_cov_err={max(cov.values()):.4f}")
for name, lo, hi in (("short", 1, 10), ("mid", 15, 45), ("long", 60, 90)):
    sel = [mae[h] for h in hs if lo <= h <= hi]
    if sel:
        print(f"METRIC mae_ratio_{name}={mean(sel):.6f}")
print(f"METRIC n_horizons={len(hs)}")
