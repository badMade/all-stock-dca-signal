# Injecting the drift-alert Slack webhook

The drift detector ([`scripts/drift_detection.py`](../scripts/drift_detection.py))
can post an immediate Slack alert when it finds drift. This page covers how to
supply the webhook URL to the detector across the runtimes that invoke it. For
what the detector does and when it fires, see
[drift_detection.md](drift_detection.md).

## Overview

- The detector reads the webhook URL at runtime from the
  `DCA_DRIFT_SLACK_WEBHOOK` environment variable (via `os.environ`), or from an
  explicit `--slack-webhook` argument.
- The webhook is a **secret** and is **never committed** — no tracked file holds
  a real URL. `deploy/*.env` is gitignored; only the `.example` template ships.
- Slack is **off by default**. An alert is only sent when you pass `--slack`
  (add `--slack-always` to also confirm clean runs). Without a configured
  webhook the detector prints a warning and continues; the exit code still
  reflects drift.
- **Pick ONE of section A or section B per stream, never both.** Every live
  task already runs the detector in-process immediately after writing its
  artifacts (`post_artifact_drift_check` in
  [`config/scheduled_tasks.json`](../config/scheduled_tasks.json)) — that's
  what section B is wiring the webhook into. Section A's cron/systemd
  templates run the exact same detector invocation again, independently, for
  the same run directory. Enabling both means every real drift fires two
  Slack alerts for one incident, and both invocations can write
  `drift_report.json`/`.md` into the same run directory at close to the same
  time (codex review discussion on commit b85d39e). Use section A only where
  the in-task check genuinely cannot be relied on (e.g. a task that might
  crash before reaching its own drift check).

## How to get a webhook URL

1. Go to <https://api.slack.com/apps> and create (or open) an app for your
   workspace.
2. Open **Incoming Webhooks** and toggle it on.
3. Click **Add New Webhook to Workspace**, choose the channel for drift alerts,
   and authorize.
4. Copy the generated `https://hooks.slack.com/services/…` URL. Treat it as a
   secret.

## Runtime setup (in priority order)

### A. cron / systemd (primary — own scheduler)

Templates live in [`deploy/`](../deploy) and cover all seven live streams
(market-open, morning, three intraday slots, midday, afternoon). Install the
secrets file once, with a restrictive mode so only the service account that
runs the detector can read the webhook (a plain `cp` inherits the umask, which
is typically world-readable):

```bash
install -m 600 deploy/drift-slack.env.example deploy/drift-slack.env
# edit deploy/drift-slack.env and paste your real webhook URL
```

`deploy/drift-slack.env` is gitignored, so the real webhook never enters git.
Anyone who can read this file can post to your alert channel, so a 0644 copy
of the tracked template is a real credential leak on a shared host (codex
review discussion_r3697106545) — keep it `600` and owned by the account below.

If your deployment sets `DCA_CRON_TRACKING_ROOT` to relocate the artifact tree
outside the checkout (`dca_paths.stream_root()` honors this per `AGENTS.md`),
uncomment and set the same variable in `deploy/drift-slack.env` —
[`deploy/run-drift-check.sh`](../deploy/run-drift-check.sh), the shared
script both the cron and systemd templates invoke, sources that file and
builds its run-directory lookup from
`${DCA_CRON_TRACKING_ROOT:-cron_tracking}`, so without it it'd look for
artifacts under the checkout instead of wherever the live task actually wrote
them (codex review discussion_r3697136664).

Both the systemd units and the crontab entries below call that one script
(`deploy/run-drift-check.sh <stream-subpath> <HHMMET-suffix>`) rather than
duplicating the logic inline. An earlier revision inlined the whole thing
directly in each systemd unit's `ExecStart=`, relying on single quotes to
protect its `$`-variables and `$(...)` from expansion — that does not work in
a systemd unit file: systemd expands every `$` in an `ExecStart=` command
line against its own environment before handing the result to a shell, quotes
or not (`systemd.service(5)`: *"Use `$$` in order to pass a literal dollar
sign"*), so the detector silently ran with an empty `--run-dir` (codex review
on commit `b85d39e`). Putting the logic in a real script file sidesteps this
entirely — systemd only touches the `ExecStart=` line itself, never a script
it invokes. See the script's own header comment for more.

The script also computes the trading-day date in `America/New_York`
regardless of the host's or cron daemon's own timezone — `date` alone would
compute the wrong calendar day on, say, an `Asia/Kolkata` host near a slot's
local midnight (codex review on commit `b85d39e`) — and, once the run
directory appears, waits for its contents to stop changing (not just for the
directory to exist) before invoking the detector: a live task can create the
directory and write `signals.csv` well before it writes `orders.csv` (e.g.
`near_close_dca.write_run_artifacts`), so directory existence alone can catch
a partially-written run and report a false drift (codex review: "wait for
artifact completion instead of directory creation").

**systemd** (preferred where available — the timers schedule in
`America/New_York` and stay correct across DST). The units run as a dedicated
unprivileged `dca-drift` account rather than root, since `WorkingDirectory`
points at your checkout and anyone who can modify it should not thereby gain
root at the next timer firing (codex review discussion_r3697106550):

```bash
# --user-group (-U) is required explicitly: on a host configured with
# USERGROUPS_ENAB no, plain useradd assigns the account to its distro default
# group instead of creating a same-named dca-drift group, and every unit's
# Group=dca-drift then fails to start (codex review on commit b85d39e).
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --user-group dca-drift
sudo chown dca-drift:dca-drift deploy/drift-slack.env  # keep it mode 600
# Grant dca-drift READ access to the checkout AND WRITE access to the run
# directories: the detector writes drift_report.json/.md into the run
# directory before it ever reaches the Slack path, so read-only access there
# makes every run raise before an alert can be sent (codex review on commit
# b85d39e). E.g. add dca-drift to a group that owns cron_tracking/ (or
# wherever $DCA_CRON_TRACKING_ROOT points) with group write permission.

# adjust the /opt/all-stock-dca install path inside the units first
sudo cp deploy/systemd/dca-drift-*.service deploy/systemd/dca-drift-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  dca-drift-market-open.timer dca-drift-morning.timer \
  dca-drift-intraday-1130.timer dca-drift-midday.timer \
  dca-drift-intraday-1330.timer dca-drift-intraday-1430.timer \
  dca-drift-afternoon.timer
```

Each timer fires at the live task's nominal start time; there is no
unit-level ordering against the external scheduler that actually runs the
live task, so the wait described above (directory appearance, then content
stability) is what stands in for that ordering (codex review
discussion_r3697106533). The timers deliberately do **not** set
`Persistent=true`: a missed trigger caught up after downtime would derive its
run directory from the wall-clock date at whenever systemd resumes it, not the
date of the trading day actually missed, so a catch-up run would check the
wrong (or a nonexistent) directory (codex review discussion_r3697106541) — a
missed check is simply skipped, consistent with this being a backup detector.

**cron** (use [`deploy/crontab.example`](../deploy/crontab.example) — `crontab -e`
and paste all seven weekday entries, including the `CRON_TZ=UTC` line at the
top). Without `CRON_TZ=UTC`, cron interprets the schedule fields in the host's
own local timezone rather than UTC, which on an America/New_York host fires
every check hours late (codex review discussion_r3697106537) — note this only
controls when cron *fires* the job; the trading-day date used inside the job
is computed in `America/New_York` by the script regardless, as described
above. cron times are UTC and must still be shifted at DST boundaries to keep
tracking ET — prefer the systemd timers to avoid this.

### B. External agent / MCP scheduler (primary)

This is what actually runs today: every live task already invokes the
detector in-process immediately after writing its artifacts. Wiring the
webhook in here means you do **not** also need section A for the same
streams — see the "pick ONE" note in the Overview above. The canonical live
invocation is recorded in
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json):

```bash
python scripts/drift_detection.py --run-dir <run_dir> \
  --json-out <run_dir>/drift_report.json --md-out <run_dir>/drift_report.md \
  --slack --fail-on-drift
```

Export `DCA_DRIFT_SLACK_WEBHOOK` from the scheduler's own secret store into the
environment of that invocation. No repo file is needed — keep the webhook in the
agent/MCP secret manager and let it inject the variable at run time.

### C. Local shell / by hand

Load the gitignored secrets file into your shell, then run the detector:

```bash
set -a; source deploy/drift-slack.env; set +a
python scripts/drift_detection.py --run-dir <run_dir> \
  --json-out <run_dir>/drift_report.json --md-out <run_dir>/drift_report.md \
  --slack --fail-on-drift
```

### D. GitHub Actions (backup only)

[`.github/workflows/drift-check.yml`](../.github/workflows/drift-check.yml) is a
backup self-test, not the production check. Set the webhook as a repository
secret named `DCA_DRIFT_SLACK_WEBHOOK`
(**Settings → Secrets and variables → Actions → New repository secret**). The
workflow injects it as `env: DCA_DRIFT_SLACK_WEBHOOK: ${{ secrets.DCA_DRIFT_SLACK_WEBHOOK }}`
and runs the detector with `--slack` against a synthetic fixture, so the real
`send_slack_alert()` path (env lookup, payload construction, URL validation,
and the send itself) is genuinely exercised, not just a hand-rolled
connectivity POST that bypasses that code (codex review
discussion_r3697106546). The fixture's run directory and ticker are named
`ci-self-test-*` / `ZZ_CI_SELF_TEST_*` so the resulting Slack message is never
mistaken for a real drift alert. Still skipped, not failed, when the secret is
unset. Read the caveats below before relying on it.

## Test your webhook

Once `DCA_DRIFT_SLACK_WEBHOOK` is set in your shell (section C), a quick
connectivity check. The URL is fed to curl via `-K -` (its config-file syntax,
read from stdin) rather than as a plain argument, since a URL passed directly
on the command line is visible to other local users through `ps` or
`/proc/<pid>/cmdline` while the request is in flight — the same posting
credential this guide otherwise keeps at `600` (codex review):

```bash
printf 'url = "%s"\n' "$DCA_DRIFT_SLACK_WEBHOOK" | curl -K - \
  -X POST -H 'Content-type: application/json' \
  --data '{"text":"DCA drift webhook test — please ignore."}'
```

A live end-to-end detector test against a real (or synthetic) run directory:

```bash
python scripts/drift_detection.py --run-dir <run_dir> --slack --slack-always
```

`--slack-always` posts even on a clean run, so you get a confirmation message
without needing a drifting run directory.

## GitHub Actions caveats

> The Actions workflow is a **backup heartbeat only**. Prefer the in-task check
> (agent/MCP scheduler) and cron/systemd. Three reasons it is not the source of
> truth:
>
> 1. **It reintroduces a standalone-cron model the repo deliberately rejects.**
>    Drift detection is designed to run *inside* each live trading task
>    immediately after it writes artifacts (see `drift_detection_policy` in
>    [`config/scheduled_tasks.json`](../config/scheduled_tasks.json)), not as a
>    separate scheduled job.
> 2. **Runners can't see production run directories.** The real
>    `cron_tracking/` directories live on the trading host and are gitignored, so
>    they never reach a GitHub runner — the workflow can only smoke-test a
>    synthetic fixture, never reconcile real orders.
> 3. **It is a new secret location.** Storing the webhook in Actions secrets adds
>    another place the secret lives (and can leak from) beyond your scheduler's
>    secret store.

## See also

- [drift_detection.md](drift_detection.md) — what the detector does, the
  detection rule, suspected-cause inference, and CLI.
