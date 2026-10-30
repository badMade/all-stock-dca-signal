# Bootstrap the Python runtime for bridge-shell prompts (Key bridge_python_alert_channels).
# The 2026-10-28 Gate 2 incident showed that a shared /tmp/uv-cache can be owned by
# another user before Python exists to write durable Gate 9 evidence. PR #2032 added
# prompt-level paths; keep the why and the fallback here so one checked-in script owns it.
# This file is sourced: never exit or leak shell-option changes into the caller.

_bridge_had_errexit=0
_bridge_had_nounset=0
case $- in *e*) _bridge_had_errexit=1 ;; esac
case $- in *u*) _bridge_had_nounset=1 ;; esac
set +e
set +u

PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

_bridge_base=${DCA_BRIDGE_UV_BASE:-/tmp}
_bridge_user=$(id -u 2>/dev/null)
_bridge_dir=$_bridge_base/uv-$_bridge_user

# A directory can exist yet be unusable by this bridge identity (the incident's
# shared-cache failure). Check every child before exporting any uv/temp destination.
# Writable is not enough: a world-writable dir another uid owns (the 2026-10-28
# `nobody` cache) passes `test -w`, yet its owner can swap the interpreter that
# `uv python find` returns. Owners come from `ls -ldn` because stat's flags differ
# between GNU and BSD; an unreadable owner counts as foreign (fail closed).
_bridge_foreign() {
    test -e "$1" || return 1
    _bridge_owner=$(ls -ldn "$1" 2>/dev/null | awk '{print $3}')
    test "$_bridge_owner" != "$_bridge_user"
}
_bridge_candidate_ok=1
for _bridge_child in "$_bridge_dir" "$_bridge_dir/python" "$_bridge_dir/cache" "$_bridge_dir/tmp"; do
    if { test -e "$_bridge_child" && test ! -w "$_bridge_child"; } || _bridge_foreign "$_bridge_child"; then
        _bridge_candidate_ok=0
    fi
done
if test "$_bridge_candidate_ok" -eq 0; then
    # Reuse one stable alternate across fresh device_bash shells; a random temp
    # directory here would make uv download Python again on every subsequent call.
    _bridge_dir=$_bridge_base/uv-$_bridge_user-alt
    _bridge_candidate_ok=1
    for _bridge_child in "$_bridge_dir" "$_bridge_dir/python" "$_bridge_dir/cache" "$_bridge_dir/tmp"; do
        if { test -e "$_bridge_child" && test ! -w "$_bridge_child"; } || _bridge_foreign "$_bridge_child"; then
            _bridge_candidate_ok=0
        fi
    done
    # Announce the alt too: a run that silently moved off the pinned dir is the
    # kind of thing an operator needs in the transcript when uv misbehaves later.
    if test "$_bridge_candidate_ok" -eq 1; then
        printf 'BRIDGE_UV_FALLBACK: %s\n' "$_bridge_dir" >&2
    fi
fi
if test "$_bridge_candidate_ok" -eq 0; then
    _bridge_fallback=$(mktemp -d "$_bridge_base/uv-$_bridge_user-XXXXXX" 2>/dev/null)
    if test -n "$_bridge_fallback"; then
        _bridge_dir=$_bridge_fallback
        printf 'BRIDGE_UV_FALLBACK: %s\n' "$_bridge_dir" >&2
    else
        _bridge_dir=
    fi
fi

if test -n "$_bridge_dir" && ! mkdir -p "$_bridge_dir/python" "$_bridge_dir/cache" "$_bridge_dir/tmp" 2>/dev/null; then
    # A missing base may still be impossible to create (for example, a read-only
    # mount); treat that like an unwritable existing cache and try the stable alt.
    _bridge_dir=$_bridge_base/uv-$_bridge_user-alt
    mkdir -p "$_bridge_dir/python" "$_bridge_dir/cache" "$_bridge_dir/tmp" 2>/dev/null
fi
if test -n "$_bridge_dir" && test ! -d "$_bridge_dir/python"; then
    _bridge_fallback=$(mktemp -d "$_bridge_base/uv-$_bridge_user-XXXXXX" 2>/dev/null)
    if test -n "$_bridge_fallback"; then
        _bridge_dir=$_bridge_fallback
        printf 'BRIDGE_UV_FALLBACK: %s\n' "$_bridge_dir" >&2
        mkdir -p "$_bridge_dir/python" "$_bridge_dir/cache" "$_bridge_dir/tmp" 2>/dev/null
    else
        _bridge_dir=
    fi
fi
if test -z "$_bridge_dir" || ! test -d "$_bridge_dir/python" || ! test -d "$_bridge_dir/cache" || ! test -d "$_bridge_dir/tmp"; then
    unset PY
    BRIDGE_BOOTSTRAP_ERROR='no writable uv directory'
    export BRIDGE_BOOTSTRAP_ERROR
    printf 'BRIDGE_UV_ERROR: no writable uv directory\n' >&2
    if test "$_bridge_had_errexit" -eq 1; then set -e; fi
    if test "$_bridge_had_nounset" -eq 1; then set -u; fi
    return 1
fi
for _bridge_child in "$_bridge_dir" "$_bridge_dir/python" "$_bridge_dir/cache" "$_bridge_dir/tmp"; do
    if test ! -w "$_bridge_child" || _bridge_foreign "$_bridge_child"; then
        unset PY
        BRIDGE_BOOTSTRAP_ERROR='no writable uv directory'
        export BRIDGE_BOOTSTRAP_ERROR
        printf 'BRIDGE_UV_ERROR: no writable uv directory\n' >&2
        if test "$_bridge_had_errexit" -eq 1; then set -e; fi
        if test "$_bridge_had_nounset" -eq 1; then set -u; fi
        return 1
    fi
done

UV_PYTHON_INSTALL_DIR=$_bridge_dir/python
UV_CACHE_DIR=$_bridge_dir/cache
TMPDIR=$_bridge_dir/tmp
export UV_PYTHON_INSTALL_DIR UV_CACHE_DIR TMPDIR
mkdir -p "$UV_PYTHON_INSTALL_DIR" "$UV_CACHE_DIR" "$TMPDIR" 2>/dev/null

PY=$(uv python find 3.11 2>/dev/null)
UV_ERR=
if test -z "$PY" || test ! -x "$PY"; then
    _bridge_install_output=$(uv python install 3.11 2>&1)
    _bridge_install_status=$?
    PY=$(uv python find 3.11 2>/dev/null)
    if test "$_bridge_install_status" -ne 0 || test -z "$PY" || test ! -x "$PY"; then
        UV_ERR=$(printf '%s\n' "$_bridge_install_output" | tail -n 5)
        printf 'UV_INSTALL_ERROR: %s\n' "$UV_ERR" >&2
        unset PY
        BRIDGE_BOOTSTRAP_ERROR='Python 3.11 installation or discovery failed'
        export BRIDGE_BOOTSTRAP_ERROR
        if test "$_bridge_had_errexit" -eq 1; then set -e; fi
        if test "$_bridge_had_nounset" -eq 1; then set -u; fi
        return 1
    fi
fi

_bridge_version=$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)
if test $? -ne 0 || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    unset PY
    BRIDGE_BOOTSTRAP_ERROR='Python 3.11 or newer is required'
    export BRIDGE_BOOTSTRAP_ERROR
    if test "$_bridge_had_errexit" -eq 1; then set -e; fi
    if test "$_bridge_had_nounset" -eq 1; then set -u; fi
    return 1
fi

export PY UV_ERR
unset BRIDGE_BOOTSTRAP_ERROR
printf 'BRIDGE_PY: %s %s\n' "$PY" "$_bridge_version"
if test "$_bridge_had_errexit" -eq 1; then set -e; fi
if test "$_bridge_had_nounset" -eq 1; then set -u; fi
return 0
