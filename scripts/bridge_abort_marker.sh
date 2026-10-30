#!/bin/sh
# Persist the part of a bridge abort that survives its short-lived device_bash shell.
# The 2026-10-28 Gate 2 incident ended before Python existed, so no normal Gate 9
# alert or failed-start evidence could be written. Keep this tiny writer Python-free:
# the marker lets verify_schedule distinguish a fired abort from a missed invocation,
# while an off-window marker deliberately does not excuse a scheduled slot.

usage() {
    printf '%s\n' 'usage: bridge_abort_marker.sh --slot SLOT --gate N --reason TEXT --started-at ISO8601 [--off-window] [--print-roots]' >&2
}

_repo_root=$(CDPATH= cd "$(dirname "$0")/.." 2>/dev/null && pwd -P) || exit 1
_slot=
_gate=
_reason=
_started=
_off_window=0
_print_roots=0
while test "$#" -gt 0; do
    case $1 in
        --slot) test "$#" -ge 2 || { usage; exit 2; }; _slot=$2; shift 2 ;;
        --gate) test "$#" -ge 2 || { usage; exit 2; }; _gate=$2; shift 2 ;;
        --reason) test "$#" -ge 2 || { usage; exit 2; }; _reason=$2; shift 2 ;;
        --started-at) test "$#" -ge 2 || { usage; exit 2; }; _started=$2; shift 2 ;;
        --off-window) _off_window=1; shift ;;
        --print-roots) _print_roots=1; shift ;;
        *) usage; exit 2 ;;
    esac
done

_trim_root() {
    # Trim trailing slashes without turning the filesystem root into an empty path.
    _trim_value=$1
    while test "${_trim_value%/}" != "$_trim_value" && test "$_trim_value" != /; do
        _trim_value=${_trim_value%/}
    done
    printf '%s' "$_trim_value"
}

_cron_root=${DCA_CRON_TRACKING_ROOT-}
if test -z "$_cron_root" && test -z "${DCA_DISABLE_DOTENV+x}"; then
    _env_file=$_repo_root/.env
    # Match dca_env._read_checked: refuse links, non-regular files, foreign
    # ownership, loose permissions, and oversized content. A bad .env is ignored;
    # parsing one is not worth making a no-Python abort writer fail open.
    if test -f "$_env_file" && test ! -L "$_env_file"; then
        _env_uid=$(id -u)
        _file_uid=$(stat -c '%u' "$_env_file" 2>/dev/null || stat -f '%u' "$_env_file" 2>/dev/null)
        _file_mode=$(stat -c '%a' "$_env_file" 2>/dev/null || stat -f '%Lp' "$_env_file" 2>/dev/null)
        _file_size=$(wc -c < "$_env_file" 2>/dev/null | tr -d ' ')
        case $_file_mode in *[!0-7]*|'') _file_mode=777 ;; esac
        if test "$_file_uid" = "$_env_uid" && test "$((0$_file_mode & 077))" -eq 0 && test "${_file_size:-4097}" -le 4096; then
            # Never source .env: it is data, and this script reads only the one
            # allowlisted root key. The last non-empty assignment wins like _parse.
            while IFS= read -r _line || test -n "$_line"; do
                _line=$(printf '%s' "$_line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
                case $_line in ''|'#'*) continue ;; esac
                case $_line in 'export '*) _line=${_line#export }; _line=$(printf '%s' "$_line" | sed 's/^[[:space:]]*//') ;; esac
                _key=${_line%%=*}
                _key=$(printf '%s' "$_key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
                if test "$_key" = DCA_CRON_TRACKING_ROOT; then
                    _value=${_line#*=}
                    _value=$(printf '%s' "$_value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
                    if test "${_value#\"}" != "$_value" && test "${_value%\"}" != "$_value"; then
                        _value=${_value#\"}; _value=${_value%\"}
                    elif test "${_value#\'}" != "$_value" && test "${_value%\'}" != "$_value"; then
                        _value=${_value#\'}; _value=${_value%\'}
                    fi
                    test -n "$_value" && _cron_root=$_value
                fi
            done < "$_env_file"
        fi
    fi
fi
test -n "$_cron_root" || _cron_root=$_repo_root/cron_tracking
_cron_root=$(_trim_root "$_cron_root")
_attempt_root=${DCA_ATTEMPT_LOG_ROOT-}
test -n "$_attempt_root" || _attempt_root=$_cron_root/attempt_log
_attempt_root=$(_trim_root "$_attempt_root")

if test "$_print_roots" -eq 1; then
    printf 'CRON_ROOT:%s\nATTEMPT_ROOT:%s\n' "$_cron_root" "$_attempt_root"
    exit 0
fi

# The mapping is repeated here because Python is precisely what may be missing at
# abort time. Keep it aligned with verify_schedule.SLOT_EVIDENCE_LAYOUT and
# dca_paths.STREAM_ARTIFACT_DIRS; the parity test makes that duplication reviewable.
case $_slot in
    0935) _stream=market_open; _artifact=market_open_dca ;;
    1030) _stream=morning; _artifact=morning_dca ;;
    1130|1330|1430) _stream=intraday; _artifact=intraday_dca ;;
    1230) _stream=midday; _artifact=midday_dca ;;
    1530) _stream=afternoon; _artifact=ce16ba6b ;;
    *) usage; exit 2 ;;
esac

# The timestamp is Gate 1's already-converted ET instant. Reject UTC spellings:
# deriving a trading-day directory from a UTC date can move an evening ET abort
# to the wrong day, while the supplied ET offset gives the correct day verbatim.
if test -z "$_started" || test -z "$_reason" || test -z "$_gate"; then usage; exit 2; fi
case $_gate in *[!0-9]*|'') usage; exit 2 ;; esac
case $_started in
    ????-??-??T??:??-04:00|????-??-??T??:??-05:00|????-??-??T??:??:??-04:00|????-??-??T??:??:??-05:00|????-??-??T??:??:??.*-04:00|????-??-??T??:??:??.*-05:00) ;;
    *) usage; exit 2 ;;
esac
printf '%s\n' "$_started" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?(-04:00|-05:00)$' || { usage; exit 2; }
_day=${_started%%T*}
_day_dir=$(printf '%s' "$_day" | tr -d '-')

# Strip control bytes before JSON quoting, redact account-shaped digit runs, then
# cap output so an injected bridge error cannot turn one marker into a large artifact.
_safe_reason=$(printf '%s' "$_reason" | tr -d '\001-\037\177' | sed -E 's/[0-9]{6,}/[REDACTED]/g' | cut -c 1-300)
_json_reason=$(printf '%s' "$_safe_reason" | sed 's/\\/\\\\/g; s/"/\\"/g')
_phase=before_run_artifact
test "$_off_window" -eq 0 || _phase=off_window
_attempt_file=$_attempt_root/$_day_dir/$_stream.attempts.jsonl
_alert_dir=$_cron_root/$_artifact/alerts
_alert_file=$_alert_dir/$_day-$_slot-gate$_gate.txt

if ! mkdir -p "$_attempt_root/$_day_dir" "$_alert_dir"; then
    printf '%s\n' 'BRIDGE_MARKER_ERROR: could not create output directories' >&2
    exit 1
fi
if ! printf '{"stream":"%s","error_class":"BridgeAbort","error_message":"%s","failure_phase":"%s","triggered_at_utc":"%s","gate":"Gate %s","slot":"%s","source":"bridge_abort_marker"}\n' \
    "$_stream" "$_json_reason" "$_phase" "$_started" "$_gate" "$_slot" >> "$_attempt_file"; then
    printf '%s\n' 'BRIDGE_MARKER_ERROR: could not append attempt marker' >&2
    exit 1
fi

# Reserve the first available canonical name, then .2, .3, ... like
# dca_alerts.write_alert_file. noclobber makes simultaneous aborts choose distinct files.
_suffix=1
while :; do
    if test "$_suffix" -eq 1; then
        _candidate=$_alert_dir/$_day-$_slot-gate$_gate.txt
    else
        _candidate=$_alert_dir/$_day-$_slot-gate$_gate.$_suffix.txt
    fi
    (umask 077; set -C; : > "$_candidate") 2>/dev/null && { _alert_file=$_candidate; break; }
    if test ! -e "$_candidate"; then
        printf '%s\n' 'BRIDGE_MARKER_ERROR: could not reserve alert file' >&2
        exit 1
    fi
    _suffix=$((_suffix + 1))
done
if ! {
    printf 'Failing gate: Gate %s\nSlot: %s\nReason: %s\nevidence: none (aborted before Python/run directory)\n' \
        "$_gate" "$_slot" "$_safe_reason" > "$_alert_file"
}; then
    printf '%s\n' 'BRIDGE_MARKER_ERROR: could not write alert file' >&2
    exit 1
fi
printf 'BRIDGE_ATTEMPT_FILE: %s\nBRIDGE_ALERT_FILE: %s\n' "$_attempt_file" "$_alert_file"
exit 0
