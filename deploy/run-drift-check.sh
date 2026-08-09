#!/usr/bin/env bash
# Shared entry point for the standalone (backup) drift-check deployment used
# by both deploy/crontab.example and deploy/systemd/*.service. See
# docs/slack_webhook_injection.md for the full setup.
#
# This logic lives in a real script file rather than inline in a systemd
# ExecStart= command. systemd expands every "$" in an ExecStart= line against
# its OWN environment before handing the result to a shell -- including
# "$(...)" command substitution and plain shell variables like "$d" -- and
# single quotes there only group words, they do not suppress that expansion
# the way they would in a real shell ("Use $$ in order to pass a literal
# dollar sign", per systemd.service(5)). A previous revision of these units
# put this whole script inline in ExecStart= and relied on single quotes to
# protect it, which does not work: systemd would try (and fail) to expand
# $root/$d/$(date ...) itself before bash ever saw them (codex review on
# commit b85d39e). Keeping this in an actual file sidesteps the whole
# problem: systemd only touches the ExecStart= line, never a script it invokes.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <stream-subpath> <HHMMET-suffix>" >&2
  echo "  e.g. $0 morning_dca 1030ET" >&2
  exit 1
fi
stream_subpath=$1
suffix=$2

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# set -e (above) means a missing/unreadable secrets file aborts here instead
# of silently continuing without a webhook and reporting a false-clean check
# (codex review discussion_r3697106543). Also carries DCA_CRON_TRACKING_ROOT
# when the operator has set it in this same file.
set -a
. deploy/drift-slack.env
set +a

root=${DCA_CRON_TRACKING_ROOT:-cron_tracking}
# Trading-day dates are always Eastern, regardless of the host's or the cron
# daemon's own timezone. CRON_TZ (see crontab.example) only controls when
# cron FIRES this job, not what `date` returns once it's running -- on a host
# whose local zone isn't America/New_York, plain `date` can report the wrong
# calendar day for a slot near local midnight (codex review on commit
# b85d39e).
date_et=$(TZ=America/New_York date +%Y%m%d)
run_dir="$root/$stream_subpath/${date_et}_${suffix}"

# This timer/cron entry fires at the live task's nominal start time, so there
# is no unit/cron-level ordering against the external scheduler that actually
# runs it. Wait (bounded) for the run directory to appear, then ALSO wait for
# the order-evidence file the detector itself reads -- submitted_orders.csv,
# or its documented fallbacks orders.csv / filled_orders.csv -- before
# inspecting the run: a directory can exist with signals.csv already written
# but that file still to come (e.g. near_close_dca.write_run_artifacts
# creates the directory and writes signals.csv well before orders.csv), so
# directory existence alone (or even a directory whose contents merely
# haven't changed in the last N seconds -- a slow writer with pauses between
# steps would fool that) can catch a partially-written run and report a false
# drift (codex review: "wait for the stream's required final artifacts...
# instead"). This second wait is deliberately NOT unbounded: a run that
# genuinely crashed before writing any order file (the exact
# raw_data_persistence_failure case this detector exists to catch) must still
# be reported once the wait elapses, not masked by waiting forever for a file
# that's never coming.
for _ in $(seq 1 30); do
  [ -d "$run_dir" ] && break
  sleep 30
done
if [ -d "$run_dir" ]; then
  # require_submitted_orders=False: the strict scheduled-run contract only
  # accepts the literal submitted_orders.csv, but this backup check -- like
  # the detector it feeds (ORDER_CSV_NAMES/load_run_artifacts in
  # scripts/drift_detection.py) -- must also finalize on the documented
  # orders.csv/filled_orders.csv fallbacks. Passing the strict default here
  # made the wait block for the full 600s grace period even when an accepted
  # fallback file was already written, because contract.ok could never turn
  # True without the exact submitted_orders.csv name (codex review
  # discussion_r3742447653).
  #
  # That alone is not enough, though: on the compact live path,
  # near_close_dca.write_run_artifacts finalizes run_state.json (including
  # the order_submit_status completion marker) BEFORE orders.csv is written
  # -- _backfill_orders_csv_usage exists specifically because orders.csv
  # "lands after write_run_artifacts has already finalized run_state.json".
  # So waiving order evidence from the contract entirely let this wait return
  # the instant run_state/signals finalize, possibly before ANY order file
  # exists, reopening the same race for the drift detector invoked right
  # after this block (codex review discussion_r3742506962 follow-up). The
  # explicit poll below closes that gap: after the run_state/signals wait
  # above returns, keep waiting -- within the SAME overall 600s budget -- for
  # one of the accepted order-evidence names to actually appear.
  RUN_DIR="$run_dir" python3 - <<'PY'
import os
import sys
import time

sys.path.insert(0, "scripts")
from scheduled_artifacts import await_finalized_run_artifacts

run_dir = os.environ["RUN_DIR"]
grace_seconds = 600.0
poll_seconds = 15.0
order_csv_names = ("submitted_orders.csv", "orders.csv", "filled_orders.csv")


def order_evidence_present() -> bool:
    return any(os.path.exists(os.path.join(run_dir, name)) for name in order_csv_names)


result = await_finalized_run_artifacts(
    run_dir,
    grace_seconds=grace_seconds,
    poll_seconds=poll_seconds,
    require_submitted_orders=False,
)

remaining = grace_seconds - result.waited_seconds
while remaining > 0 and not order_evidence_present():
    time.sleep(min(poll_seconds, remaining))
    remaining -= poll_seconds
PY
fi

# Two distinct ways this backup check can find a run that never really
# happened, neither of which the ordinary detector invocation below reports
# on its own -- so both send a best-effort Slack alert directly through the
# detector's own send_slack_alert()/DriftReport (imported, not reimplemented,
# so the same webhook-safety validation applies) and exit 1, instead of
# falling through to a detector run that would stay silent or report clean:
#
#   1. The run directory never appeared at all. Invoking the detector on a
#      nonexistent --run-dir just exits 1 without a report or a Slack alert
#      (scripts/drift_detection.py:1024-1026) (codex review: "alert when the
#      run directory never appears").
#   2. The directory exists but NEITHER a signals CSV nor an order-evidence
#      CSV ever appeared -- the live task created the directory and then
#      crashed before writing anything into it. detect_drift() finds zero
#      expected buys when no signals CSV exists at all, so drift_detected
#      is False and this backup check would otherwise report a false "clean"
#      for a run that never actually ran (codex review: "treat an order-file
#      timeout as a failed run"). This is deliberately narrower than "orders
#      never appeared": when signals.csv DOES exist but no order evidence
#      does, that's exactly the submitted_orders_missing/raw_data_persistence
#      case the ordinary detector invocation below already correctly flags,
#      so it isn't treated as a failure here.
alert_failed_run() {
  RUN_DIR="$run_dir" CAUSE="$1" ACTION="$2" python3 - <<'PY' || true
import os
import sys
import urllib.error

sys.path.insert(0, "scripts")
from drift_detection import DriftReport, send_slack_alert  # noqa: E402

report = DriftReport(
    run_dir=os.environ["RUN_DIR"],
    drift_detected=True,
    affected_symbols=[],
    run_suspected_cause=os.environ["CAUSE"],
    recommended_action=os.environ["ACTION"],
    signals_present=False,
    orders_present=False,
    submitted_orders_empty=True,
    raw_dir_present=False,
    expected_symbol_count=0,
    submitted_symbol_count=0,
)
try:
    if not send_slack_alert(report):
        print("warning: no Slack webhook configured; failed-run alert not sent", file=sys.stderr)
except (urllib.error.URLError, OSError) as exc:
    print(f"warning: failed-run Slack alert failed: {exc}", file=sys.stderr)
PY
}

if [ ! -d "$run_dir" ]; then
  echo "error: run directory never appeared within the wait window: $run_dir" >&2
  alert_failed_run "run_directory_missing" \
    "This backup check's wait window elapsed and the live task's run directory never appeared. Verify the live task actually ran for this slot -- it may have crashed before writing any artifacts."
  exit 1
fi

if [ ! -f "$run_dir/signals.csv" ] && [ ! -f "$run_dir/morning_signals.csv" ] \
   && [ ! -f "$run_dir/midday_signals.csv" ] && [ ! -f "$run_dir/market_open_signals.csv" ] \
   && [ ! -f "$run_dir/intraday_signals.csv" ] \
   && [ ! -f "$run_dir/submitted_orders.csv" ] && [ ! -f "$run_dir/orders.csv" ] \
   && [ ! -f "$run_dir/filled_orders.csv" ]; then
  echo "error: run directory exists but has neither a signals CSV nor an order-evidence CSV: $run_dir" >&2
  alert_failed_run "no_artifacts_written" \
    "The live task's run directory exists but the wait window elapsed with neither a signals CSV nor an order-evidence CSV present. The task likely crashed immediately after creating the directory; the ordinary detector run below would find zero expected buys and report clean, so this backup check is flagging it directly."
  exit 1
fi

exec python3 scripts/drift_detection.py \
  --run-dir "$run_dir" \
  --json-out "$run_dir/drift_report.json" \
  --md-out "$run_dir/drift_report.md" \
  --slack --fail-on-drift
