#!/usr/bin/env bash
# Benchmark for the octopus 1..90-day autoresearch loop. Fixed universe/dates/horizons; the loop edits
# octopus_model.py and autoresearch.knobs (octopus hyperparameters) only. Lower mae_ratio is better.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-$HOME/.cache/all-stock-dca/ci-venvs/wondrous-octopus/bin/python}
SRC="$HOME/Library/CloudStorage/GoogleDrive-geoffplymale@gmail.com/My Drive/Tickers"
# Measured 2026-11-19: 31 tickers, 2018+, 2024-01..2025-06 cost ~18.5 min fit + ~9 s/step (~75 min total), too slow for a loop;
# 12 tickers / 2021+ / 7 months targets ~15 min. h=90 needs >90 steps to mature, hence the 7-month window.
# Widened 2026-11-19 to 48 tickers (the 12-ticker baseline was unstable: mae_ratio 5.0, see autoresearch.md).
# Bounded history: the initial fit trains on every row before the cutoff, which for 1980s-era archives
# took >19 min with no output. A fixed 2022+ copy keeps the benchmark short and identical across runs.
HD=${AR_HIST:-/tmp/ar_hist48}
TICKERS="AAPL MSFT NVDA JPM XOM JNJ PG KO HD CAT INTC T AMZN GOOGL META BAC WMT CVX PEP DIS CSCO ORCL MRK ABT VZ MCD NKE BA GE IBM UNH LLY V MA COST TMO LIN ADBE TXN QCOM HON UPS LOW SBUX MDT AMGN GS DE"
if [ ! -f "$HD/DE.csv" ]; then
  mkdir -p "$HD"
  for t in $TICKERS; do (head -1 "$SRC/$t.csv"; awk -F, 'NR>1 && $1>="2022-01-01"' "$SRC/$t.csv") > "$HD/$t.csv"; done
fi
RUN=${AR_RUN_DIR:-/tmp/ar_run}
KNOBS=$(grep -v '^#' autoresearch.knobs | tr '\n' ' ')
rm -rf "$RUN"
$PY -u run_walkforward.py --run-dir "$RUN" --start 2024-04-01 --end 2024-09-30 --model octopus \
  --max-horizon 90 --eval-horizons 2,3,5,7,10,15,20,30,45,60,90 --diagnostic-sample 100 \
  --checkpoint-every 1000 --history-dir "$HD" --device mps --seed 0 $KNOBS \
  --tickers $TICKERS ${EXTRA:-} \
  > "$RUN.log" 2>&1
PERSIST=${AR_PERSIST:-/tmp/ar_persist48}
if [ ! -f "$PERSIST/report/report.json" ]; then
  $PY -u run_walkforward.py --run-dir "$PERSIST" --start 2024-04-01 --end 2024-09-30 --model persistence \
    --max-horizon 90 --eval-horizons 2,3,5,7,10,15,20,30,45,60,90 --diagnostic-sample 100 \
    --checkpoint-every 1000 --history-dir "$HD" --device mps --seed 0 --window-len 128 --update-every 5 \
    --tickers $TICKERS > "$PERSIST.log" 2>&1
fi
$PY autoresearch_metric.py "$RUN/report/report.json" "$PERSIST/report/report.json"
