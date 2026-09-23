#!/bin/bash
# Pre-fetch the 3:30 PM yfinance fallback payload, outside any Cowork session.
# Runs at 15:31 ET: the 15:15 start-stamped bar Gate 5 needs is only complete
# at 15:30, so an earlier run would hand the generator a still-forming bar.
set -euo pipefail

: "${DCA_REPO:?set DCA_REPO to the all-stock-dca checkout}"
cd "$DCA_REPO"

DAY="${DCA_TRADING_DAY:-$(TZ=America/New_York date +%F)}"

WINDOW_STATUS=0
ROOT="$(
DAY="$DAY" python3 - <<'PY'
import os
from datetime import date

import dca_config
import dca_paths
import dca_runner

day = date.fromisoformat(os.environ["DAY"])
status = dca_runner.market_day_status(day)
if status.is_closed or status.is_early_close:
    raise SystemExit(4)
approved = dca_config.load_approved_dates(dca_config.get_config_dir()).get("approved_test_dates")
if not approved or day not in approved:
    raise SystemExit(3)
print(dca_paths.stream_root("ce16ba6b", "scheduled_runs", f"{day.isoformat()}_1530ET"))
PY
)" || WINDOW_STATUS=$?
if [ "$WINDOW_STATUS" -eq 3 ]; then
  echo "prefetch_1530: $DAY is outside config/trading_window.json; skipping"
  exit 0
fi
if [ "$WINDOW_STATUS" -eq 4 ]; then
  echo "prefetch_1530: $DAY is not a regular trading day for the 3:30 slot; skipping"
  exit 0
fi
[ "$WINDOW_STATUS" -eq 0 ] || exit "$WINDOW_STATUS"

# Same root the 3:30 prompt resolves, derived by the same validation that
# confirmed this day is an approved regular 3:30 trading session.
mkdir -p "$ROOT"

# Gate 5's universe: active, minus excluded, minus whole-share, minus GFUZ (Robinhood-primary).
# Through dca_config.load_tickers, the loader the generator's own config path
# uses: it rejects duplicate keys and malformed categories and honours
# $DCA_CONFIG_DIR, so a bad tickers.json fails here instead of silently
# fetching a different universe than Gate 5 scores.
TICKERS="$(python3 -c "
import dca_config
t = dca_config.load_tickers(dca_config.get_config_dir())
if not t.get('tickers'):
    raise SystemExit('tickers.json has no active tickers')
skip = set(t.get('excluded_tickers', ())) | set(t.get('whole_share_tickers', ())) | {'GFUZ'}
print(','.join(s for s in t['tickers'] if s not in skip))
")"

OUT="$ROOT/yfinance_fallback_${DAY}_1530.json"
LOCKDIR="$ROOT/.prefetch_1530.lock"
PREFETCH_PYTHON="${DCA_PREFETCH_PYTHON:-$DCA_REPO/yfinance-prefetch/.venv/bin/python}"
PREFETCH_FETCH="${DCA_PREFETCH_FETCH_SCRIPT:-$DCA_REPO/yfinance-prefetch/fetch_yfinance_fallback.py}"
# A unique temp file in the same directory, renamed into place: the 3:30
# prompt may run this script itself as a recovery step while the launchd run
# is still writing, and a shared fixed temp name would let the two interleave
# or publish a truncated file. Same directory keeps the mv an atomic rename.
cleanup() {
  [ -n "${TMP:-}" ] && rm -f "$TMP"
  [ -n "${LOCKDIR:-}" ] && rmdir "$LOCKDIR" 2>/dev/null || true
}
trap cleanup EXIT
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "prefetch_1530: another run is already publishing $OUT; skipping"
  exit 0
fi
if [ -e "$OUT" ]; then
  echo "prefetch_1530: $OUT already exists; leaving the existing fallback in place"
  exit 0
fi
TMP="$(mktemp "$(dirname "$OUT")/prefetch_1530.XXXXXX")"
"$PREFETCH_PYTHON" "$PREFETCH_FETCH" \
  --tickers "$TICKERS" --trading-day "$DAY" --decision-time-et 15:30 --interval-minutes 15 --out "$TMP"
if ln "$TMP" "$OUT" 2>/dev/null; then
  rm -f "$TMP"
  TMP=""
else
  echo "prefetch_1530: $OUT appeared during fetch; leaving the published fallback in place"
  exit 0
fi
