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
  *) echo "refused: not_rootfs"; exit 2 ;;  # never delete anything but the root disk
esac

if [ "${1:-}" != "--force" ]; then
  case "$(date +%u)" in
    6|7) ;;
    *) echo "refused: weekday"; exit 2 ;;
  esac
fi

claude_running() { pgrep -x Claude >/dev/null 2>&1; }

if claude_running; then
  osascript -e 'quit app "Claude"' >/dev/null 2>&1
  waited=0
  while claude_running; do
    if [ "$waited" -ge "$WAIT" ]; then
      echo "refused: claude_still_running"
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
