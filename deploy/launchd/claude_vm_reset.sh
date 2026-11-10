#!/bin/sh
# Weekly Cowork VM reset (operator request 2026-11-04). Run by
# com.all-stock-dca.claude-vm-reset.plist.template every Saturday at 03:00.
#
# Why: on 2026-11-04 every Claude slot aborted at Gate 2 because the Cowork
# bridge VM's 9.6 GB root disk was full (a 2.7 GB /tmp/out left by an earlier
# session, owned by `nobody`, undeletable from inside: no sudo). That disk is
# one file on the Mac, rootfs.img, and it survives app restarts. Deleting it
# while Claude is quit made the app build a fresh VM on the next start
# (verified 2026-11-04 20:23 ET). This does that once a week.
#
# Why: on 2026-11-09 all seven slots were missed while Claude stayed quit from
# 2026-11-08 22:24 to 2026-11-09 19:12. A late quit after the timeout used to
# leave Claude closed, so the refusal path now reopens it.
#
# Safety:
#   - Saturday/Sunday only, unless --force: no slot runs on a weekend, so it
#     can never interrupt a trade or a backup.
#   - Never deletes while Claude is still running (a live VM disk).
#   - Deletes exactly one file, rootfs.img; the bundle's other image (the
#     session workspaces) is never named here, so it can never be removed.
#
# Prints one status line; exit 0 done, 1 Claude would not quit, 2 weekday.

set -u

DISK="${CLAUDE_VM_DISK:-$HOME/Library/Application Support/Claude/vm_bundles/claudevm.bundle/rootfs.img}"
WAIT="${CLAUDE_VM_QUIT_WAIT:-60}"

case "$(basename "$DISK")" in
  rootfs.img) ;;
  *) echo "refused: not_rootfs $(date '+%Y-%m-%dT%H:%M:%S%z')"; exit 2 ;;  # never delete anything but the root disk
esac

if [ "${1:-}" != "--force" ]; then
  case "$(date +%u)" in
    6|7) ;;
    *) echo "refused: weekday $(date '+%Y-%m-%dT%H:%M:%S%z')"; exit 2 ;;
  esac
fi

# -a (2026-11-09 review): macOS pgrep drops its own ancestors from the match list. Run by hand from a shell that a
# Claude session started, a plain `pgrep -x Claude` misses the open app, so this script would skip the quit and
# delete the root disk of a live VM. -a keeps ancestors in the match, so a running Claude is always seen.
claude_running() { pgrep -a -x Claude >/dev/null 2>&1; }

if claude_running; then
  # stderr stays on (2026-11-08): the 11-08 run stopped at claude_still_running with no reason because osascript's
  # error went to /dev/null. A refused quit (e.g. "Not authorized to send Apple events", -1743) now lands in the log.
  osascript -e 'quit app "Claude"' >/dev/null
  waited=0
  while claude_running; do
    if [ "$waited" -ge "$WAIT" ]; then
      # 2026-11-09: all seven slots were missed while Claude stayed quit from
      # 2026-11-08 22:24 to 2026-11-09 19:12; a late quit after timeout used
      # to leave Claude closed. Reopen it before refusing, without deleting the disk.
      open -a Claude
      echo "refused: claude_still_running $(date '+%Y-%m-%dT%H:%M:%S%z')"
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
fi

if [ -e "$DISK" ]; then
  rm -f "$DISK"
  result="reset: deleted"
else
  result="reset: no_disk"
fi

open -a Claude
echo "$result $(date '+%Y-%m-%dT%H:%M:%S%z')"
