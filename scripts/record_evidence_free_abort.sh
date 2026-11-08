#!/bin/sh
# Write the channel-2 alert file and an evidence-free abort marker for a slot that aborts before any
# run evidence can exist -- with no Python 3.11 required.
#
# Why this exists (2026-10-29). Two abort paths left nothing durable behind:
#   * Gate 1 off-window: scripts/finalize_run_evidence.py refuses a start outside the slot's
#     acceptance window (an off-window fire is not a scheduled fire), so no run_state.json is written;
#   * Gate 2 GATE2_ABORT_NO_PYTHON311 when the bridge has no usable python3 at all.
# Only the push alert got out. The repo alert file (Gate 9 channel 2) was never written because every
# prompt resolved its path with `python3 -c "import dca_paths; ..."` late in Gate 9, and the schedule
# watchdog read the empty slot as `slot_did_not_fire` -- "the scheduler never ran it" -- when the slot
# had in fact fired and aborted. This script is called FIRST on those paths, before anything else:
#
#     sh scripts/record_evidence_free_abort.sh --slot 1130 --gate gate1 --reason GATE1_OFF_WINDOW <<'ALERT'
#     <the same alert text the other two channels carry>
#     ALERT
#
# It writes:
#   1. <cron root>/<stream dir>/alerts/<YYYY-MM-DD>-<slot>-<gate>.txt -- the path the prompt's own Gate 9
#      channel 2 uses, with dca_alerts.write_alert_file's collision numbering (.2.txt, .3.txt), which
#      scripts/alert_digest.py reads;
#   2. one JSON line appended to <cron root>/abort_markers/<YYYYMMDD>/<slot>.jsonl, which
#      scripts/abort_markers.py reads so scripts.schedule_watchdog can report "fired, aborted before
#      evidence" instead of `slot_did_not_fire`. The marker is never run evidence: it never verifies a
#      slot and is never read by budget, cap or same-day allocation code.
#
# The cron root comes from dca_paths through whatever python3 the shell has (the bridge's 3.10 is
# enough -- dca_paths is stdlib-only and loads the operator's .env for DCA_CRON_TRACKING_ROOT). Only
# when there is no python3 at all does it fall back to an exported $DCA_CRON_TRACKING_ROOT, else the
# checkout's own cron_tracking/ (dca_paths' default), and say so on stderr: this script never reads
# .env itself.
#
# Long digit runs (account-shaped) and e-mail addresses in the body are redacted before writing; the
# prompts' SECRET RULE still applies to what goes in.
#
# stdout: `ALERT_FILE=<path>` and `ABORT_MARKER=<path>`. Exit 0 when both were written, 1 when either
# write failed (the other may still have succeeded), 2 on bad arguments.

set -u

usage() {
    printf 'usage: %s --slot HHMM --gate TOKEN --reason CODE [--fired-at-utc YYYY-MM-DDTHH:MM:SSZ] < body\n' "$0" >&2
    exit 2
}

SLOT= GATE= REASON= FIRED_AT=
while [ $# -gt 0 ]; do
    # `shift 2` past the end is fatal in dash, so require the value first.
    [ $# -ge 2 ] || usage
    case $1 in
        --slot) SLOT=$2; shift 2 ;;
        --gate) GATE=$2; shift 2 ;;
        --reason) REASON=$2; shift 2 ;;
        --fired-at-utc) FIRED_AT=$2; shift 2 ;;
        *) usage ;;
    esac
done

# Slot -> the dir whose alerts/ each live prompt's Gate 9 channel 2 already writes to (the 3:30 prompt
# uses ce16ba6b/scheduled_runs/alerts), which is also scripts/verify_schedule.SLOT_EVIDENCE_LAYOUT's
# run-dir parent; scripts/tests/test_record_evidence_free_abort.py pins all three together.
case $SLOT in
    0935) STREAM_DIR=market_open_dca ;;
    1030) STREAM_DIR=morning_dca ;;
    1130|1330|1430) STREAM_DIR=intraday_dca ;;
    1230) STREAM_DIR=midday_dca ;;
    1530) STREAM_DIR=ce16ba6b/scheduled_runs ;;
    *) printf 'error: --slot must be one of 0935 1030 1130 1230 1330 1430 1530\n' >&2; usage ;;
esac
# The gate token must match scripts/alert_digest._ALERT_FILENAME's <gate> group, or the digest
# drops the filename's date and slot.
printf '%s' "$GATE" | grep -Eq '^[A-Za-z0-9_]+(\.[0-9]+)?$' || { printf 'error: bad --gate token\n' >&2; usage; }
printf '%s' "$REASON" | grep -Eq '^[A-Z0-9_]+$' || { printf 'error: --reason must be an UPPER_SNAKE code\n' >&2; usage; }

GIVEN_FIRED_AT=$FIRED_AT
if [ -z "$FIRED_AT" ]; then
    FIRED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
fi
printf '%s' "$FIRED_AT" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' ||
    { printf 'error: --fired-at-utc must be YYYY-MM-DDTHH:MM:SSZ\n' >&2; usage; }

REPO=$(cd "$(dirname "$0")/.." && pwd)

# The ET trading day names both files. The conversion is checked by its offset, exactly as Gate 1
# checks its own clock: a missing zoneinfo database makes `TZ=America/New_York date` print UTC
# silently, and a UTC date after 20:00 ET would file the alert under tomorrow.
ET_DAY= ET_OFFSET=
if command -v python3 >/dev/null 2>&1; then
    ET_LINE=$(cd "$REPO" && python3 -c "
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
t = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=ZoneInfo('UTC'))
e = t.astimezone(ZoneInfo('America/New_York'))
print(e.strftime('%Y-%m-%d %z'))" "$FIRED_AT" 2>/dev/null)
    ET_DAY=${ET_LINE% *} ET_OFFSET=${ET_LINE#* }
fi
if [ -z "$ET_DAY" ]; then
    # GNU date (the bridge VM) parses -d; BSD date does not. "Now" is only a valid stand-in when no
    # --fired-at-utc was given (FIRED_AT was just sampled as now); an explicit one must not be replaced.
    ET_LINE=$(TZ=America/New_York date -d "$FIRED_AT" '+%Y-%m-%d %z' 2>/dev/null) ||
        { [ -z "$GIVEN_FIRED_AT" ] && ET_LINE=$(TZ=America/New_York date '+%Y-%m-%d %z'); } || ET_LINE='? ?'
    ET_DAY=${ET_LINE% *} ET_OFFSET=${ET_LINE#* }
fi
case $ET_OFFSET in
    -0400|-0500) ;;
    *) printf 'error: could not convert %s to America/New_York (offset %s)\n' "$FIRED_AT" "$ET_OFFSET" >&2; exit 1 ;;
esac

ROOT=
if command -v python3 >/dev/null 2>&1; then
    ROOT=$(cd "$REPO" && python3 -c "import dca_paths; print(dca_paths.cron_tracking_root())" 2>/dev/null)
fi
if [ -z "$ROOT" ]; then
    ROOT=${DCA_CRON_TRACKING_ROOT:-$REPO/cron_tracking}
    printf 'warning: no python3 to resolve dca_paths; using %s (a DCA_CRON_TRACKING_ROOT set only in .env is not seen)\n' "$ROOT" >&2
fi
ROOT=${ROOT%/}

STATUS=0

ALERT_DIR="$ROOT/$STREAM_DIR/alerts"
BODY=$(sed -E -e 's/[0-9]{9,}/[REDACTED]/g' -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[REDACTED_EMAIL]/g')
ALERT=
if mkdir -p "$ALERT_DIR" 2>/dev/null; then
    n=1
    while :; do
        if [ "$n" -eq 1 ]; then
            candidate="$ALERT_DIR/$ET_DAY-$SLOT-$GATE.txt"
        else
            candidate="$ALERT_DIR/$ET_DAY-$SLOT-$GATE.$n.txt"
        fi
        # noclobber makes the create atomic: two writers never share one numbered file.
        if (set -C; printf '%s\n' "$BODY" > "$candidate") 2>/dev/null; then
            ALERT=$candidate
            break
        fi
        [ -e "$candidate" ] || break
        n=$((n + 1))
        [ "$n" -le 100 ] || break
    done
fi
if [ -n "$ALERT" ]; then
    printf 'ALERT_FILE=%s\n' "$ALERT"
else
    printf 'error: alert file under %s could not be written\n' "$ALERT_DIR" >&2
    STATUS=1
fi

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

MARKER_DIR="$ROOT/abort_markers/$(printf '%s' "$ET_DAY" | tr -d -)"
MARKER="$MARKER_DIR/$SLOT.jsonl"
LINE=$(printf '{"alert_file": "%s", "fired_at_utc": "%s", "gate": "%s", "kind": "evidence_free_abort", "reason_code": "%s", "slot_id": "%s", "trading_day": "%s", "writer": "scripts/record_evidence_free_abort.sh"}' \
    "$(json_escape "$ALERT")" "$FIRED_AT" "$GATE" "$REASON" "$SLOT" "$ET_DAY")
if mkdir -p "$MARKER_DIR" 2>/dev/null && printf '%s\n' "$LINE" >> "$MARKER" 2>/dev/null; then
    printf 'ABORT_MARKER=%s\n' "$MARKER"
else
    printf 'error: abort marker %s could not be written\n' "$MARKER" >&2
    STATUS=1
fi

exit "$STATUS"
