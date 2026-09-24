#!/bin/bash
# Pre-fetch one live slot's labeled yfinance fallback payload, outside any Cowork session.
#
#   prefetch_slot.sh <0935|1030|1130|1230|1330|1430|1530>
#   prefetch_slot.sh auto      # the slot whose decision time passed 1-14 minutes ago (launchd)
#
# Why (2026-09-22): the five Robinhood-primary slots' only data path was the
# live session pulling ~64 tickers of 5-minute bars through MCP tool output,
# which blew the tool output cap and the session's token budget (Notion DCA
# Issues: output_size_limit, intraday_bars_unavailable, token_limit_exceeded,
# market_data_unavailable); and whether a session can reach yfinance itself
# varies by session. Every slot's Gate 5 generator now accepts this payload as
# its fallback (config/scheduled_tasks.json market_data_source_policy), so it
# is fetched here, on a schedule, in its own venv. See deploy/launchd/PREFETCH.md.
#
# Runs one minute after the slot's decision time: Gate 5 anchors on the last
# COMPLETED start-stamped bar, which only closes at the decision time, and the
# fetch keeps every bar stamped at or before it -- an earlier run would hand
# the generator a still-forming bar as if it were complete.
set -euo pipefail

: "${DCA_REPO:?set DCA_REPO to the all-stock-dca checkout}"
SLOT_ARG="${1:?usage: prefetch_slot.sh <slot HHMM|auto>}"
cd "$DCA_REPO"

DAY="${DCA_TRADING_DAY:-$(TZ=America/New_York date +%F)}"
NOW_ET="${DCA_PREFETCH_NOW_ET:-$(TZ=America/New_York date +%H:%M)}"

PLAN_STATUS=0
PLAN="$(DAY="$DAY" NOW_ET="$NOW_ET" SLOT_ARG="$SLOT_ARG" python3 deploy/launchd/prefetch_plan.py)" || PLAN_STATUS=$?
case "$PLAN_STATUS" in
  0) ;;
  3) echo "prefetch_slot: $DAY is outside config/trading_window.json; skipping"; exit 0 ;;
  4) echo "prefetch_slot: $DAY is not a trading day for slot $SLOT_ARG (closed or early close); skipping"; exit 0 ;;
  5) echo "prefetch_slot: no slot is due at $NOW_ET ET; skipping"; exit 0 ;;
  *) exit "$PLAN_STATUS" ;;
esac

IFS=$'\t' read -r SLOT ROOT DECISION INTERVAL TICKERS <<<"$PLAN"
mkdir -p "$ROOT"

OUT="$ROOT/yfinance_fallback_${DAY}_${SLOT}.json"
LOCKDIR="$ROOT/.prefetch_${SLOT}.lock"
PREFETCH_PYTHON="${DCA_PREFETCH_PYTHON:-$DCA_REPO/yfinance-prefetch/.venv/bin/python}"
PREFETCH_FETCH="${DCA_PREFETCH_FETCH_SCRIPT:-$DCA_REPO/yfinance-prefetch/fetch_yfinance_fallback.py}"
# A unique temp file in the same directory, published with ln (which fails if
# the name exists): the slot's prompt may run this script itself as a recovery
# step while the launchd run is still writing, and a shared fixed temp name
# would let the two interleave or publish a truncated file.
cleanup() {
  [ -n "${TMP:-}" ] && rm -f "$TMP"
  [ -n "${LOCKDIR:-}" ] && rmdir "$LOCKDIR" 2>/dev/null || true
}
trap cleanup EXIT
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  LOCKDIR=""
  echo "prefetch_slot: another run is already publishing $OUT; skipping"
  exit 0
fi
if [ -e "$OUT" ]; then
  echo "prefetch_slot: $OUT already exists; leaving the existing fallback in place"
  exit 0
fi
TMP="$(mktemp "$ROOT/prefetch_${SLOT}.XXXXXX")"
"$PREFETCH_PYTHON" "$PREFETCH_FETCH" \
  --tickers "$TICKERS" --trading-day "$DAY" --decision-time-et "$DECISION" --interval-minutes "$INTERVAL" --out "$TMP"
if ln "$TMP" "$OUT" 2>/dev/null; then
  rm -f "$TMP"
  TMP=""
  echo "prefetch_slot: wrote $OUT"
else
  echo "prefetch_slot: $OUT appeared during fetch; leaving the published fallback in place"
  exit 0
fi
