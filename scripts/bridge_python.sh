#!/bin/sh
# Resolve (installing if needed) a Python 3.11 interpreter for the device_bash bridge VM.
#
# Why this exists (2026-10-29). The seven live executor prompts each carried the same ~600-character
# inline opener that pinned uv's install dir, cache and TMPDIR under /tmp and ran
# `uv python find 3.11` / `uv python install 3.11`. On 2026-10-28 the 11:30 and 3:30 slots aborted
# GATE2_ABORT_NO_PYTHON311 because the pinned cache dir was owned by `nobody` and uv got
# "permission denied"; the routines were re-saved with per-uid dirs, but that fix was still untested
# shell copied seven times with no way out when the pinned dir is unusable. This script is the one
# checked-in copy every prompt calls, with that way out (a fallback dir) and tests
# (scripts/tests/test_bridge_python.py) that run it against a fake `uv`.
#
# POSIX sh and coreutils only: it runs BEFORE any Python 3.11 exists, on a bridge VM whose own
# python3 is 3.10.
#
# Usage, at the start of every device_bash call (each call is a fresh shell):
#     cd $HOME/mnt/all-stock-dca && export PYTHONDONTWRITEBYTECODE=1 && PY=$(sh scripts/bridge_python.sh)
#
# Disk reclaim (2026-11-04), for the prompts' BRIDGE DISK PREFLIGHT when /tmp is under 1 GiB free:
#     sh scripts/bridge_python.sh --reclaim
# On 2026-11-04 the 9:35, 2:30 and 3:30 slots aborted at Gate 2 with 68 MiB free on /tmp even after the
# preflight's `env -u UV_CACHE_DIR uv cache clean; rm -rf "$HOME/.cache/uv"`. That cleanup only clears uv's
# DEFAULT cache, which this script never uses: every install writes its download cache and temp files under
# $BASE/uv-<uid>/{cache,tmp}, and every fallback is another whole $BASE/uv-<uid>-XXXXXX dir with its own
# Python (~90 MiB) and cache, so failed or repeated runs pile up where nothing ever cleans. --reclaim:
#   * empties cache/ and tmp/ in every dir of THIS uid (pinned and fallbacks), after `uv cache clean`;
#   * keeps the one Python 3.11 install the next call will reuse (the pinned dir's if it has one, else the
#     first fallback that has one) and deletes every other fallback dir of this uid;
#   * clears uv's default cache too (the old preflight step), and never touches another uid's dirs or
#     anything outside $BASE and $HOME/.cache/uv -- never the mounted repo folder.
# It prints `BRIDGE_RECLAIM:` lines (df before/after, each dir removed) on stderr, nothing on stdout, exit 0.
#
# stdout: exactly one line, the absolute interpreter path, and only on success.
# stderr: `BRIDGE_PYTHON: ...` notes (e.g. which fallback dir was used) and, on failure, one
#         `BRIDGE_PYTHON_ERROR: <reason>` line plus `UV_INSTALL_ERROR: ...` lines (the last lines of
#         uv's own install error) so the Gate 2 alert can say WHY (disk, network, TLS, permissions).
# exit:   0 ok; 3 uv not on PATH; 4 no usable working dir; 5 uv could not find or install 3.11;
#         6 the interpreter uv returned is not Python 3.11+.
#
# Environment (tests and operators only; the prompts set none of these):
#     DCA_BRIDGE_UV_BASE   parent of the pinned per-uid dir (default /tmp; the bridge VM's roomy `/`
#                          filesystem -- $HOME is a near-full /sessions mount and the mounted repo
#                          folder rejects uv's archive extraction, verified 2026-10-16)

set -u

BASE=${DCA_BRIDGE_UV_BASE:-/tmp}
BASE=${BASE%/}
[ -n "$BASE" ] || BASE=/
UID_NOW=$(id -u)

note() { printf 'BRIDGE_PYTHON: %s\n' "$*" >&2; }
fail() {
    code=$1
    shift
    printf 'BRIDGE_PYTHON_ERROR: %s\n' "$*" >&2
    exit "$code"
}

# Numeric owner uid of a path, portable across GNU and BSD (`stat` flags differ; `ls -n` does not).
owner_of() { ls -ldn "$1" 2>/dev/null | awk '{print $3}'; }

# Why a working dir cannot be used, or nothing when it can. A dir is usable when it and its three
# subdirs exist (created here if missing), are owned by this uid, and accept a write. Ownership is
# checked, not just -w: a world-writable dir someone else owns is exactly the 2026-10-28 shape
# (/tmp/uv-cache owned by `nobody`), and uv refuses a cache it does not own.
unusable_reason() {
    dir=$1
    for path in "$dir" "$dir/python" "$dir/cache" "$dir/tmp"; do
        if [ ! -e "$path" ]; then
            mkdir -p "$path" 2>/dev/null || { echo "$path cannot be created"; return; }
            chmod 700 "$path" 2>/dev/null
        fi
        [ -d "$path" ] || { echo "$path is not a directory"; return; }
        owner=$(owner_of "$path")
        [ "$owner" = "$UID_NOW" ] || { echo "$path is owned by uid ${owner:-unknown}, not $UID_NOW"; return; }
        { [ -w "$path" ] && [ -x "$path" ]; } || { echo "$path is not writable"; return; }
    done
    probe="$dir/tmp/.bridge-python-probe.$$"
    if ! : > "$probe" 2>/dev/null; then
        echo "$dir/tmp rejects writes"
        return
    fi
    rm -f "$probe"
}

# Fallback dirs are `mktemp -d $BASE/uv-<uid>-XXXXXX`. A later fresh shell reuses one it already owns
# instead of making another, so Python 3.11 (~90 MiB) is installed once per VM, not once per call.
find_fallback() {
    for candidate in "$BASE/uv-$UID_NOW"-*; do
        [ -d "$candidate" ] || continue
        [ -z "$(unusable_reason "$candidate")" ] && { echo "$candidate"; return; }
    done
    # Always under $BASE, never a bare `mktemp -d`: the bare form lands wherever the platform puts
    # temp dirs, where the reuse glob above would never find it again.
    made=$(mktemp -d "$BASE/uv-$UID_NOW-XXXXXX" 2>/dev/null) || return
    [ -z "$(unusable_reason "$made")" ] && echo "$made"
}

use_dir() {
    export UV_PYTHON_INSTALL_DIR="$1/python" UV_CACHE_DIR="$1/cache" TMPDIR="$1/tmp"
}

# Sets PY to the interpreter, or leaves PY empty and the install error in UV_ERR. Called directly,
# never inside $(...): a subshell would drop UV_ERR, the one thing the Gate 2 alert needs.
find_or_install() {
    UV_ERR=
    PY=$(uv python find 3.11 2>/dev/null)
    if [ -z "$PY" ]; then
        UV_ERR=$(uv python install 3.11 2>&1 | tail -n 5)
        PY=$(uv python find 3.11 2>/dev/null)
    fi
}

free_line() { df -Pk "$BASE" 2>/dev/null | awk 'NR>1 {printf "%s %d MiB free\n", $6, $4/1024}'; }

# 0 when the install dir $1 holds a Python 3.11 uv can find. Subshell, so the caller's UV_* stay as they were.
has_311() ( UV_PYTHON_INSTALL_DIR="$1" uv python find 3.11 >/dev/null 2>&1 )

reclaim() {
    printf 'BRIDGE_RECLAIM: before: %s\n' "$(free_line)" >&2
    keep=
    for dir in "$BASE/uv-$UID_NOW" "$BASE/uv-$UID_NOW"-*; do
        [ -d "$dir" ] || continue
        [ "$(owner_of "$dir")" = "$UID_NOW" ] || continue
        if [ -d "$dir/cache" ]; then
            UV_CACHE_DIR="$dir/cache" uv cache clean >/dev/null 2>&1
            rm -rf "$dir/cache" && mkdir -p "$dir/cache" && chmod 700 "$dir/cache"
        fi
        [ -d "$dir/tmp" ] && rm -rf "$dir/tmp" && mkdir -p "$dir/tmp" && chmod 700 "$dir/tmp"
        [ -z "$keep" ] && command -v uv >/dev/null 2>&1 && has_311 "$dir/python" && keep=$dir
    done
    for dir in "$BASE/uv-$UID_NOW"-*; do
        [ -d "$dir" ] || continue
        [ "$dir" = "$keep" ] && continue
        [ "$(owner_of "$dir")" = "$UID_NOW" ] || continue
        rm -rf "$dir" && printf 'BRIDGE_RECLAIM: removed %s\n' "$dir" >&2
    done
    env -u UV_CACHE_DIR uv cache clean >/dev/null 2>&1
    rm -rf "$HOME/.cache/uv"
    [ -n "$keep" ] && printf 'BRIDGE_RECLAIM: kept the Python 3.11 install in %s\n' "$keep" >&2
    printf 'BRIDGE_RECLAIM: after: %s\n' "$(free_line)" >&2
    exit 0
}

[ "${1-}" = "--reclaim" ] && reclaim

command -v uv >/dev/null 2>&1 || fail 3 "uv is not on PATH in the bridge shell"

PINNED="$BASE/uv-$UID_NOW"
REASON=$(unusable_reason "$PINNED")
FALLBACK=
if [ -n "$REASON" ]; then
    FALLBACK=$(find_fallback)
    [ -n "$FALLBACK" ] || fail 4 "pinned dir unusable ($REASON) and no writable fallback dir could be made"
    note "pinned dir unusable ($REASON); using $FALLBACK"
    use_dir "$FALLBACK"
else
    use_dir "$PINNED"
fi

find_or_install
if [ -z "$PY" ] && [ -z "$FALLBACK" ]; then
    # The pinned dir looked fine but uv still failed (e.g. a stale, half-extracted install another
    # session left behind). One retry in a fresh dir costs a download; skipping it costs the slot.
    FIRST_ERR=$UV_ERR
    FALLBACK=$(find_fallback)
    if [ -n "$FALLBACK" ]; then
        note "uv failed in $PINNED; retrying in $FALLBACK"
        use_dir "$FALLBACK"
        find_or_install
    fi
    [ -n "$UV_ERR" ] || UV_ERR=$FIRST_ERR
fi

if [ -z "$PY" ]; then
    [ -n "$UV_ERR" ] && printf '%s\n' "$UV_ERR" | sed 's/^/UV_INSTALL_ERROR: /' >&2
    fail 5 "uv could not find or install Python 3.11 (UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR)"
fi

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null ||
    fail 6 "$PY is not Python 3.11 or newer"

printf '%s\n' "$PY"
