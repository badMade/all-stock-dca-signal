#!/bin/bash
# Claude app watchdog (operator request 2026-11-09). Run every 5 minutes by
# com.all-stock-dca.claude-app-watchdog.plist.template.
#
# Why: on Monday 2026-11-09 the Claude desktop app was not running from
# 22:25 ET Sunday until 19:12 ET Monday (the Mac itself stayed awake). The
# seven live DCA slots are Claude scheduled tasks, so none fired in its
# window; when the app came back they all fired at once and aborted
# GATE1_OFF_WINDOW. Nothing was bought that day.
#
# What it does: inside the guarded window (Mon-Fri 08:30-16:00 America/
# New_York, so the 9:35 slot always has a running app and the 3:30 slot's
# window is covered to its end) it checks whether Claude is running and, if
# not, opens it and raises one macOS notification. Outside the window it does
# nothing, so quitting Claude in the evening or at the weekend sticks.
#
# Safety:
#   - Never quits, restarts or signals Claude: it only ever opens it when no
#     Claude process exists. A running app, healthy or not, is left alone.
#   - Weekend-only claude_vm_reset.sh quits Claude on Saturday 03:00; this
#     script never acts on a weekend, so the two can never fight.
#   - The window is computed in America/New_York regardless of the Mac's own
#     time zone (launchd itself only understands local time).
#
# A refused notification (osascript's error) goes to stderr, i.e. the agent's log.
# Prints one status line; exit 0 always unless the relaunch fails (exit 1).

set -u

WAIT="${CLAUDE_APP_START_WAIT:-60}"
WINDOW_START="${CLAUDE_APP_WINDOW_START:-0830}"
WINDOW_END="${CLAUDE_APP_WINDOW_END:-1600}"

stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }

weekday="$(TZ=America/New_York date +%u)"
hhmm="$(TZ=America/New_York date +%H%M)"

if [ "${1:-}" != "--force" ]; then
  case "$weekday" in
    1|2|3|4|5) ;;
    *) echo "skip: weekend $(stamp)"; exit 0 ;;
  esac
  # Leading zeros: compare as base-10 integers, not octal.
  now=$(expr "$hhmm" + 0)
  start=$(expr "$WINDOW_START" + 0)
  end=$(expr "$WINDOW_END" + 0)
  if [ "$now" -lt "$start" ] || [ "$now" -ge "$end" ]; then
    echo "skip: outside_window $(stamp)"
    exit 0
  fi
fi

# ps, not pgrep: pgrep returned nothing from a sandboxed shell on 2026-11-09 while
# Claude was running, which would make this script open Claude every 5 minutes.
# `ps -axco comm` prints bare executable names; "Claude Helper ..." never matches.
claude_running() { ps -axco comm= | grep -qx Claude; }

if claude_running; then
  echo "ok: running $(stamp)"
  exit 0
fi

open -a Claude
waited=0
while ! claude_running; do
  if [ "$waited" -ge "$WAIT" ]; then
    osascript -e 'display notification "Claude was not running and did not start. DCA slots will not fire." with title "DCA watchdog" sound name "Basso"' >/dev/null
    echo "failed: did_not_start $(stamp)"
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done

osascript -e 'display notification "Claude was not running during market hours; reopened it." with title "DCA watchdog"' >/dev/null
echo "relaunched: after ${waited}s $(stamp)"
