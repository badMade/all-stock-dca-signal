#!/bin/bash
# Build one live slot's Robinhood market-data payload ahead of its Gate 5, outside any Claude session.
#
#   robinhood_payload.sh <0935|1030|1130|1230|1330|1430>
#   robinhood_payload.sh 1530      # shadow build only: 'auto' never selects it and no prompt reads it yet
#   robinhood_payload.sh auto      # the slot whose decision time passed 1-14 minutes ago (launchd)
#
# Why (2026-10-15): repo code cannot call Robinhood (headless clients cannot get tokens), and the live
# sessions could not pull ~67 tickers of bars through MCP tool output inside the slot window, so the 11:30,
# 1:30 and 2:30 runs aborted at Gate 5. The Codex CLI on this Mac holds an approved Robinhood MCP connection
# (`codex mcp list` -> robinhood-trading), so it does the read-only fetch here, a few tickers per call, and
# scripts/robinhood_payload.py validates every number before anything is published. See
# docs/robinhood_payload.md. This is NOT a second data source: the payload is Robinhood data under
# sources.robinhood, exactly what the session would have fetched itself.
#
# Runs one minute after the slot's decision time (the last completed start-stamped bar closes then).
set -euo pipefail

: "${DCA_REPO:?set DCA_REPO to the all-stock-dca checkout}"
SLOT_ARG="${1:?usage: robinhood_payload.sh <slot HHMM|auto>}"
cd "$DCA_REPO"

PY="${DCA_PAYLOAD_PYTHON:-$DCA_REPO/.venv/bin/python}"
CODEX="${DCA_CODEX_BIN:-codex}"
# Unset = the model in ~/.codex/config.toml. Model names the installed CLI/account rejects change between
# releases (gpt-5.6-sol worked on 0.145, is refused on 0.160), so pin one only via DCA_CODEX_MODEL.
MODEL_ARGS=()
[ -n "${DCA_CODEX_MODEL:-}" ] && MODEL_ARGS=(-m "$DCA_CODEX_MODEL")
PER_CHUNK_TIMEOUT="${DCA_CODEX_CHUNK_TIMEOUT:-240}"
PARALLEL="${DCA_CODEX_PARALLEL:-3}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/dca-rhpayload.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

DAY_ARGS=()
[ -n "${DCA_TRADING_DAY:-}" ] && DAY_ARGS=(--day "$DCA_TRADING_DAY")
NOW_ARGS=()
[ -n "${DCA_PAYLOAD_NOW_ET:-}" ] && NOW_ARGS=(--now-et "$DCA_PAYLOAD_NOW_ET")

PLAN_STATUS=0
"$PY" -m scripts.robinhood_payload plan --slot "$SLOT_ARG" ${DAY_ARGS[@]+"${DAY_ARGS[@]}"} ${NOW_ARGS[@]+"${NOW_ARGS[@]}"} >"$WORK/plan.json" || PLAN_STATUS=$?
case "$PLAN_STATUS" in
  0) ;;
  3) echo "robinhood_payload: day is outside config/trading_window.json; skipping"; exit 0 ;;
  4) echo "robinhood_payload: not a trading day for slot $SLOT_ARG (closed or early close); skipping"; exit 0 ;;
  5) echo "robinhood_payload: no slot is due now; skipping"; exit 0 ;;
  *) exit "$PLAN_STATUS" ;;
esac

OUT="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["out"])' "$WORK/plan.json")"
if [ -e "$OUT" ]; then
  # Reuse only a payload that is provably fresh for THIS slot (built after the decision time, within the age
  # bound, matching its sidecar checksum). A leftover from an earlier attempt is set aside, never overwritten
  # or scored, and the payload is rebuilt (2026-10-15 adversarial review).
  if "$PY" -m scripts.robinhood_payload check --plan "$WORK/plan.json" >/dev/null 2>"$WORK/reuse.err"; then
    echo "robinhood_payload: $OUT already exists and is fresh; leaving it in place"
    exit 0
  fi
  echo "robinhood_payload: existing payload is not reusable ($(tr '\n' ' ' <"$WORK/reuse.err")); setting it aside and rebuilding" >&2
  STAMP="$(date +%s)"
  mv "$OUT" "${OUT%.json}.stale-$STAMP.json"
  [ -e "${OUT%.json}.meta.json" ] && mv "${OUT%.json}.meta.json" "${OUT%.json}.stale-$STAMP.meta.json"
fi

# One codex call per chunk of tickers, a few at a time. A chunk that times out or errors simply leaves its
# tickers without data; the merge records them in ticker_errors and enforces the coverage floor.
run_chunk() {
  local idx="$1" tickers="$2"
  "$PY" -m scripts.robinhood_payload prompt --plan "$WORK/plan.json" --tickers "$tickers" >"$WORK/prompt_$idx.txt"
  # Read-only sandbox: the worker only reads market data. stdin is the prompt (never inherit a terminal).
  # macOS has no timeout(1); perl's alarm + exec kills the worker at the deadline (SIGALRM).
  # One retry per chunk (2026-10-19: a first pass left 16/68 tickers with a truncated reply or a bad quote stamp
  # and the slot aborted). The retry runs only when the first reply is missing or carries a per-ticker error.
  local attempt out="$WORK/chunk_$idx.json" best="$WORK/best_$idx.json" errs best_errs=""
  for attempt in 1 2; do
    rm -f "$out"
    # Per-attempt wall time (2026-10-22): nobody had measured a real 90-ticker build, so build-time work could not be
    # sized. Whole seconds from date +%s, which macOS's bash 3.2 and date both support.
    local started; started="$(date +%s)"
    perl -e 'alarm shift; exec @ARGV' "$PER_CHUNK_TIMEOUT" "$CODEX" exec ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} -c 'sandbox_mode="read-only"' -c 'approval_policy="never"' \
      --skip-git-repo-check -o "$out" - <"$WORK/prompt_$idx.txt" >"$WORK/chunk_$idx.log" 2>&1 \
      || echo "robinhood_payload: chunk $idx attempt $attempt failed or timed out" >&2
    if [ ! -s "$out" ]; then
      echo "robinhood_payload: timing chunk=$idx attempt=$attempt seconds=$(( $(date +%s) - started )) errors=n/a" >&2
    else
      # grep -o exits 1 when a clean reply has no "error"; under set -e + pipefail that silently ended this worker
      # (the reply file survived only because nothing had deleted it yet), so a clean reply is counted as 0 here.
      errs="$(grep -o '"error"' "$out" | wc -l | tr -d ' ' || true)"
      echo "robinhood_payload: timing chunk=$idx attempt=$attempt seconds=$(( $(date +%s) - started )) errors=$errs" >&2
      # Keep the attempt with the fewest per-ticker errors, so a worse retry never replaces a better first pass.
      if [ -z "$best_errs" ] || [ "$errs" -lt "$best_errs" ]; then cp "$out" "$best"; best_errs="$errs"; fi
      [ "$errs" -eq 0 ] && break
    fi
  done
  if [ -s "$best" ]; then cp "$best" "$out"; else rm -f "$out"; fi
  [ -s "$WORK/chunk_$idx.json" ] || echo "robinhood_payload: chunk $idx failed or timed out (see its tickers in the merge report)" >&2
}

BUILD_STARTED="$(date +%s)"
idx=0
while IFS= read -r tickers; do
  run_chunk "$idx" "$tickers" &
  idx=$((idx + 1))
  # Bound concurrency without bash 4's wait -n (macOS ships bash 3.2).
  if [ $((idx % PARALLEL)) -eq 0 ]; then wait; fi
done < <("$PY" -c 'import json,sys; [print(",".join(c)) for c in json.load(open(sys.argv[1]))["chunks"]]' "$WORK/plan.json")
wait

shopt -s nullglob
# The total covers the fetch and the merge; the merge's exit status still decides the launcher's.
MERGE_STATUS=0
"$PY" -m scripts.robinhood_payload merge --plan "$WORK/plan.json" "$WORK"/chunk_*.json || MERGE_STATUS=$?
echo "robinhood_payload: timing total seconds=$(( $(date +%s) - BUILD_STARTED )) chunks=$idx parallel=$PARALLEL" >&2
exit "$MERGE_STATUS"
