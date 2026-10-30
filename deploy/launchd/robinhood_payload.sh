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
# Opt-in overall build deadline, in seconds from the start of the fetch (2026-10-22). Unset/0 = none, so the six
# scheduled slots behave exactly as before. A real 90-ticker build takes 2-4 minutes, but nothing bounded the worst
# case (12 chunks in 4 lock-step waves, each up to two 240 s attempts): a slow Mac would deliver a payload after the
# slot's own window. With a deadline each attempt's timeout is capped to the time left, no attempt starts with less
# than DCA_MIN_ATTEMPT_SECONDS left, and the merge then sees the missing tickers and enforces its coverage floor,
# so a late build fails fast at Gate 5 instead of arriving too late to be read.
BUILD_DEADLINE_SECONDS="${DCA_BUILD_DEADLINE_SECONDS:-0}"
MIN_ATTEMPT_SECONDS="${DCA_MIN_ATTEMPT_SECONDS:-20}"
# Tickers per request in the rescue pass (2026-10-26, see below): small enough that one rescue request that still dies
# costs fewer tickers than the 95% floor tolerates (4 of a 91-ticker universe).
RESCUE_SIZE="${DCA_RESCUE_CHUNK_SIZE:-2}"
case "$BUILD_DEADLINE_SECONDS$MIN_ATTEMPT_SECONDS$PARALLEL$RESCUE_SIZE" in
  ""|*[!0-9]*) echo "robinhood_payload: DCA_BUILD_DEADLINE_SECONDS, DCA_MIN_ATTEMPT_SECONDS, DCA_CODEX_PARALLEL and DCA_RESCUE_CHUNK_SIZE must be whole numbers" >&2; exit 2 ;;
esac
[ "$RESCUE_SIZE" -gt 0 ] || { echo "robinhood_payload: DCA_RESCUE_CHUNK_SIZE must be at least 1" >&2; exit 2; }

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
# Where a dead worker's own output is kept (2026-10-26): $WORK, and every chunk_<i>.log in it, is deleted on exit, so the
# 09:35 refusal (chunks 4 and 10 dead on both attempts) left nothing to say why. Only attempts that returned no reply
# are kept, so a healthy build writes nothing here. Gitignored like the rest of the cron tree.
KEEP_LOGS="${OUT%.json}.chunk_logs"
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
# Seconds left before the opt-in build deadline; empty when none is set. BUILD_STARTED is assigned before the first
# chunk starts, and each chunk worker is a subshell that reads it, so every worker measures from the same instant.
seconds_left() {
  if [ "$BUILD_DEADLINE_SECONDS" -gt 0 ]; then echo $(( BUILD_DEADLINE_SECONDS - ( $(date +%s) - BUILD_STARTED ) )); fi
}

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
    local left attempt_timeout="$PER_CHUNK_TIMEOUT"
    left="$(seconds_left)"
    if [ -n "$left" ]; then
      if [ "$left" -lt "$MIN_ATTEMPT_SECONDS" ]; then
        echo "robinhood_payload: deadline chunk=$idx attempt=$attempt skipped seconds_left=$left" >&2
        break
      fi
      [ "$left" -lt "$attempt_timeout" ] && attempt_timeout="$left"
    fi
    perl -e 'alarm shift; exec @ARGV' "$attempt_timeout" "$CODEX" exec ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} -c 'sandbox_mode="read-only"' -c 'approval_policy="never"' \
      --skip-git-repo-check -o "$out" - <"$WORK/prompt_$idx.txt" >"$WORK/chunk_$idx.log" 2>&1 \
      || echo "robinhood_payload: chunk $idx attempt $attempt failed or timed out" >&2
    if [ ! -s "$out" ]; then
      echo "robinhood_payload: timing chunk=$idx attempt=$attempt seconds=$(( $(date +%s) - started )) errors=n/a" >&2
      # Best-effort: a log that cannot be kept must never change the build's outcome.
      { mkdir -p "$KEEP_LOGS" && cp "$WORK/chunk_$idx.log" "$KEEP_LOGS/chunk_$idx.attempt$attempt.$BUILD_STARTED.log"; } 2>/dev/null \
        || echo "robinhood_payload: could not keep the log of chunk $idx attempt $attempt" >&2
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
"$PY" -c 'import json,sys; [print(",".join(c)) for c in json.load(open(sys.argv[1]))["chunks"]]' "$WORK/plan.json" >"$WORK/chunks.txt"
idx=0
while IFS= read -r tickers; do
  run_chunk "$idx" "$tickers" &
  idx=$((idx + 1))
  # Bound concurrency without bash 4's wait -n (macOS ships bash 3.2).
  if [ $((idx % PARALLEL)) -eq 0 ]; then wait; fi
done <"$WORK/chunks.txt"
wait

# Rescue pass (2026-10-26). The 95% floor tolerates 4 misses in a 91-ticker universe, but the unit that fails is a whole
# 8-ticker chunk: at 09:35 chunks 4 and 10 died on both attempts, 16 tickers went missing, and the slot aborted although
# every other chunk validated. A chunk with no reply at all is asked for again in requests of RESCUE_SIZE tickers (each
# with run_chunk's own retry and deadline), so one dead worker no longer costs the slot. A rescue part is named
# chunk_<i>.r<k>.json and folded back into chunk_<i>.json before the merge, so the merge's chunk ownership is unchanged.
# Extended the same day (13:30 refusal: 8 tickers, "no data returned", coverage 83/91): a chunk that DID reply but left
# tickers out, or answered them with a per-ticker error, only got run_chunk's whole-chunk retry, which keeps the better
# attempt and can still lose the same tickers twice. Those tickers are now re-asked for alone, in the same small
# requests. Only absent or "error" tickers are rescued; a ticker that replied with data is never re-fetched, and a
# rescue reply that is again missing or an error leaves the original entry in place for the merge to report.
# Extended 2026-10-30 (10-29 15:31 shadow build): chunk 3 (NVDA, QBTS, FLKR, INTC, GOOGL, GOOG, AMZN, GLW) replied with
# every ticker's intraday_bars empty, so all eight were filed "quiet" and published as NO_DATA although NVDA, GOOGL, GOOG
# and AMZN trade every minute. A whole chunk with no bars is a worker that dropped the series, so it is re-asked like a
# dead chunk. A chunk where only some names are empty (the thin names of 10-26/27 13:30) is left alone: those are quiet.
targets=()
for ((i = 0; i < idx; i++)); do
  # A crash here only means this chunk is not rescued (set -e must not end the launcher before the merge).
  need="$("$PY" -c '
import sys
from scripts.robinhood_payload import _load_chunk
try:
    reply = _load_chunk(sys.argv[1])
except (OSError, ValueError):  # no file, or one that holds no usable object: every ticker of the chunk is missing
    reply = {}
tickers = [t for t in sys.argv[2].split(",") if t]
need = [t for t in tickers if t not in reply or (isinstance(reply[t], dict) and reply[t].get("error"))]
answered = [reply[t] for t in tickers if t not in need]
if answered and all(isinstance(e, dict) and not e.get("intraday_bars") for e in answered):
    need = tickers  # every answered ticker came back with no bars: the worker dropped the series
print(",".join(need))
' "$WORK/chunk_$i.json" "$(sed -n "$((i + 1))p" "$WORK/chunks.txt")" || true)"
  if [ -n "$need" ]; then echo "$need" >"$WORK/need_$i.txt"; targets+=("$i"); fi
done
rescued=0
for i in ${targets[@]+"${targets[@]}"}; do
  IFS=, read -r -a members <<<"$(cat "$WORK/need_$i.txt")"
  echo "robinhood_payload: rescue chunk=$i tickers=${#members[@]} size=$RESCUE_SIZE" >&2
  for ((k = 0; k * RESCUE_SIZE < ${#members[@]}; k++)); do
    group="$(IFS=,; echo "${members[*]:k*RESCUE_SIZE:RESCUE_SIZE}")"
    echo "$group" >"$WORK/rescue_$i.r$k.tickers"
    run_chunk "$i.r$k" "$group" &
    rescued=$((rescued + 1))
    if [ $((rescued % PARALLEL)) -eq 0 ]; then wait; fi
  done
done
wait
for i in ${targets[@]+"${targets[@]}"}; do
  specs=()
  for t in "$WORK"/rescue_"$i".r*.tickers; do
    part="${t%.tickers}"; part="${part##*/rescue_}"
    specs+=("$WORK/chunk_$part.json=$(cat "$t")")
  done
  # Each part counts only for its own group, as each chunk does in the merge: a key a part was not asked for is dropped.
  # The chunk's own reply (if any) is the base; a part overrides it only with an entry that carries no error, so a
  # rescue can never make a ticker worse than it was.
  "$PY" -c '
import json, sys
from scripts.robinhood_payload import _load_chunk
try:
    merged = _load_chunk(sys.argv[1])
except (OSError, ValueError):
    merged = {}
changed = False
for spec in sys.argv[2:]:
    path, group = spec.rsplit("=", 1)
    try:
        reply = _load_chunk(path)
    except (OSError, ValueError):  # a dead part leaves its tickers to the merge report, as a dead chunk does
        continue
    for t in group.split(","):
        if t in reply and not (isinstance(reply[t], dict) and reply[t].get("error")):
            merged[t] = reply[t]
            changed = True
        elif t in reply and t not in merged:
            merged[t] = reply[t]  # keep the error text for the merge report when there was nothing before it
            changed = True
if changed:
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        json.dump(merged, handle)
' "$WORK/chunk_$i.json" "${specs[@]}"     || echo "robinhood_payload: rescue chunk=$i could not fold its parts back; its tickers go to the merge report" >&2
  # Under set -e a crash above would have ended the launcher before the merge (no refusal marker, no log line), so
  # every failure here only leaves the chunk as it was before the rescue.
  rm -f "$WORK"/chunk_"$i".r*.json
  # Tickers carried by the folded reply, per-ticker errors included; the merge decides which of them validate.
  echo "robinhood_payload: rescue chunk=$i replied=$("$PY" -c 'import json,sys
try: print(len(json.load(open(sys.argv[1]))))
except (OSError, ValueError): print(0)' "$WORK/chunk_$i.json" || echo 0)" >&2
done

shopt -s nullglob
# The total covers the fetch and the merge; the merge's exit status still decides the launcher's.
MERGE_STATUS=0
"$PY" -m scripts.robinhood_payload merge --plan "$WORK/plan.json" "$WORK"/chunk_*.json || MERGE_STATUS=$?
echo "robinhood_payload: timing total seconds=$(( $(date +%s) - BUILD_STARTED )) chunks=$idx parallel=$PARALLEL deadline=$BUILD_DEADLINE_SECONDS" >&2

# Plan-first half (2026-10-26): the retired 5-minute Claude courier's order-history and buying-power read, now a second
# `codex exec` after the payload is published, followed by `ingest`, which writes the recommendation files. Opt-in
# (DCA_PLAN_FIRST_INGEST=1) so the six Gate 5 slots are unchanged until the operator has seen one real run. Strictly
# best-effort: Gate 5 reads the payload only, so a broker or ingest failure is logged and never changes the exit status.
if [ "${DCA_PLAN_FIRST_INGEST:-0}" = "1" ] && [ "$MERGE_STATUS" -eq 0 ]; then
  BROKER_REPLY="$WORK/broker_reply.json"
  INGEST_STATUS=0
  {
    "$PY" -m scripts.robinhood_payload broker-prompt --plan "$WORK/plan.json" >"$WORK/broker_prompt.txt" \
      && perl -e 'alarm shift; exec @ARGV' "$PER_CHUNK_TIMEOUT" "$CODEX" exec ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} \
        -c 'sandbox_mode="read-only"' -c 'approval_policy="never"' --skip-git-repo-check -o "$BROKER_REPLY" - \
        <"$WORK/broker_prompt.txt" >"$WORK/broker.log" 2>&1 \
      && "$PY" -m scripts.robinhood_payload ingest --plan "$WORK/plan.json" --broker-reply "$BROKER_REPLY"
  } || INGEST_STATUS=$?
  [ "$INGEST_STATUS" -eq 0 ] || echo "robinhood_payload: plan-first ingest did not complete (status $INGEST_STATUS); the payload is unaffected" >&2
fi
exit "$MERGE_STATUS"
