#!/usr/bin/env bash
# Benchmark for the octopus 1..90-day autoresearch loop. Fixed universe/dates/horizons; the loop edits
# octopus_model.py and autoresearch.knobs (octopus hyperparameters) only. Lower mae_ratio is better.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-$HOME/.cache/all-stock-dca/ci-venvs/wondrous-octopus/bin/python}
SRC="$HOME/Library/CloudStorage/GoogleDrive-geoffplymale@gmail.com/My Drive/Tickers"
# Bounded history: the initial fit trains on every row before the cutoff, which for 1980s-era archives
# took >19 min with no output. A fixed 2018+ copy keeps the benchmark short and identical across runs.
HD=${AR_HIST:-/tmp/ar_hist}
TICKERS="AAPL MSFT AMZN GOOGL NVDA META JPM XOM JNJ PG KO PEP WMT HD CVX BAC DIS INTC CSCO ORCL MRK ABT T VZ MCD NKE CAT BA GE IBM UNH"
if [ ! -f "$HD/UNH.csv" ]; then
  mkdir -p "$HD"
  for t in $TICKERS; do (head -1 "$SRC/$t.csv"; awk -F, 'NR>1 && $1>="2018-01-01"' "$SRC/$t.csv") > "$HD/$t.csv"; done
fi
RUN=${AR_RUN_DIR:-/tmp/ar_run}
KNOBS=$(grep -v '^#' autoresearch.knobs | tr '\n' ' ')
rm -rf "$RUN"
$PY -u run_walkforward.py --run-dir "$RUN" --start 2024-01-01 --end 2025-06-30 --model octopus \
  --max-horizon 90 --eval-horizons 2,3,5,7,10,15,20,30,45,60,90 --diagnostic-sample 100 \
  --checkpoint-every 1000 --history-dir "$HD" --device mps --seed 0 $KNOBS \
  --tickers $TICKERS $EXTRA \
  > "$RUN.log" 2>&1
$PY autoresearch_metric.py "$RUN/report/report.json"
