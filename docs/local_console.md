# Local DCA console

Launch from the repository with Python 3.11 or later:

```bash
python3 from.py
```

This opens `http://127.0.0.1:8765`, starts an isolated DCA demo and an Octopus
baseline replay, and displays progress while they run. The DCA test uses the
existing `run_dca.py --demo --report` entry point. The Octopus test uses its
existing synthetic 2019–2020 archive, followed by the daily-update entry point.
Forecasts on this first screen are synthetic research examples, with their real
artifact dates shown. They are not current market predictions.

The core interface has no third-party runtime dependencies. If the repository
`.venv` exists, jobs use its Python automatically. Otherwise they use the
launcher's interpreter. Octopus needs its separate dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -r wondrous-octopus/requirements.txt
python3 from.py --check
```

Installation is manual; the console does not install packages. You can instead
point `--research-python` at an existing Python with NumPy, pandas and
scikit-learn. The neural Octopus smoke test also needs PyTorch. Missing research
dependencies leave the core interface usable, disable the individual research
buttons, and mark the combined demo as incomplete in its job log.

## Everyday interface

- Daily summaries: select a date, inspect observed runs, statuses, candidate
  counts and submitted-order counts, and download the summary as JSON.
- Confirmed spend: comes from the existing purchase-history reader. Requested
  amounts and simulated fills do not count as confirmed live spending. Missing
  price or fill evidence displays as unavailable, not a made-up zero.
- Schedule: Eastern Time clock, configured task times and countdowns, with
  market holidays, early closes, disabled tasks and registration gaps visible.
  Configuration describes intent; it does not establish that an external
  scheduler fired or that an order executed.
- Forecasts: adjusted-dollar open/high/low/close predictions and p10/p90 bands,
  ticker filtering and source dates. A pending target without a known date is
  labeled as the next trading row. The latest research run is independent of
  the selected historical DCA date.
- Historical periods: measured model errors, coverage and skill versus
  persistence where recorded. Only periods actually present in the report have
  results; the synthetic demo does not cover all historical crises.
- Jobs: real command output, start and finish times, failures and retained
  history. One job runs at a time, with bounded logs and execution deadlines.
- Program directory: the repository's canonical entry-point catalog plus the
  adjacent research projects. Commands requiring market-data exports, an archive,
  broker adapters, or optional services retain those setup requirements.

The browser refreshes about every 15 seconds. While the launcher is running,
its background reader also saves the current day's digest without requiring an
open browser tab. The daily JSON and Markdown summaries live in
`<console-output>/summaries/<demo-or-local>/<YYYY-MM-DD>.*`. Selecting an older
day regenerates that day's summary from the artifacts currently available.
These are evolving summaries, not immutable historical snapshots. Stopping the
launcher stops refreshes; it does not install an OS or Codex scheduled task.

## Connect existing data

```bash
python3 from.py --local
python3 from.py --local --root /path/to/cron_tracking --octopus-run /path/to/octopus/run
```

The default artifact root follows `dca_paths.cron_tracking_root()` and
`DCA_CRON_TRACKING_ROOT`. Console output normally uses
`dca_paths.stream_root('console')`; `--output-dir` overrides it. Demo output
always goes below `<console-output>/demo` and is excluded from the local daily
view. `--root` can select a mounted or synchronized copy of external run
artifacts. Streams relocated outside that root must be inspected through their
own root; this interface does not silently discover or combine arbitrary paths.
On a network-mounted `--root`, the recursive artifact `rglob` on each
15-second refresh is the hot spot; prefer a local synchronized copy if that scan
becomes slow.

Local mode observes existing output and opens without launching a demo. The
**Update Octopus day** button invokes `daily_update.py` on the supplied run,
using its recorded archive. Refresh that archive through the existing ticker
maintenance workflow first. An initial model run must already exist:

```bash
.venv/bin/python wondrous-octopus/run_walkforward.py --preset quick --dry-run
.venv/bin/python wondrous-octopus/run_walkforward.py --preset quick --run-dir /path/to/octopus/run
```

The estimate is read-only. The second command starts real research training
and may take substantial time depending on the archive and hardware. It is a
separate deliberate command, not part of console startup.

The console does not schedule or submit live brokerage orders. The existing
seven-slot executor still needs broker/data callables wired through
`dca_runner` as documented in [live_wiring.md](live_wiring.md). The seven-slot
test action exercises the existing candidate-generator regression suites.
Notion, alerts, remote archive refreshes, and other external integrations keep
their existing connector and authorization requirements. Octopus predictions
remain research artifacts and never automatically change live sizing.

## Simple entry points for testing

```bash
# Browser demo; choose an available port automatically.
python3 from.py --port 0

# Full DCA + Octopus demo, summaries and JSON result, then exit.
python3 from.py --once --output-dir /tmp/dca-console-demo

# Just the DCA demo (works with Python's standard library).
python3 from.py --run demo-dca --output-dir /tmp/dca-console-demo

# Actual PyTorch Octopus smoke test: ten replay days, with a time budget.
python3 from.py --run demo-neural --output-dir /tmp/dca-console-demo

# Test all seven signal-generation entry points using existing tests.
python3 from.py --run test-signals

# Save a summary of real local artifacts without starting any execution job.
python3 from.py --local --once --date 2026-09-04 --root /path/to/cron_tracking

# Export consolidated confirmed purchases and save the daily digest.
python3 from.py --local --run reports --root /path/to/cron_tracking

# Foreground LAN access; read "Remote access" before using this plaintext mode.
python3 from.py --local --no-browser --bind 192.0.2.10 --auth-file "$AUTH"

# Accept an HTTPS proxy name on the loopback listener.
python3 from.py --local --no-browser --auth-file "$AUTH" --trusted-host mac.tailnet.ts.net

# Inspect readiness, or start without opening a browser.
python3 from.py --check
python3 from.py --no-browser
```

`--bind ADDR` accepts an IPv4 literal and defaults to `127.0.0.1`.
`--trusted-host HOST[:PORT]` is repeatable, lowercases the value and accepts a
hostname without a scheme, path, wildcard or whitespace; an optional port must
not exceed `65535`. Add `--behind-proxy` only when the health probe below shows
the configured name in `forwarded_host`; that flag is valid only on a loopback
bind with at least one trusted host. `--auth-file PATH` is the only credential
source; any `--trusted-host` value, like any non-loopback bind, requires it. The
remote-access runbooks below cover the required combinations and
safer rollout order.

An explicit demo `--date` must pass the existing strategy date/window gates.
The launcher defaults to a recent full market session and rejects a nominally
successful DCA demo if no run-state artifact was produced. Single-action and
`--once` commands exit nonzero when requested work fails or is incomplete.
Ctrl+C stops the server and requests termination of its active child process.
Each demo invocation gets a new directory; previous runs are preserved.

## Local API

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/health` | Unauthenticated `status`, `mode`, `received_host` and `forwarded_host`; Host, Origin and Fetch Metadata checks still apply |
| GET | `/api/bootstrap` | Basic-authenticated session token and default date, rolling only in unpinned local mode |
| GET | `/api/snapshot?date=YYYY-MM-DD&ticker=AAPL&period=all` | Basic-authenticated daily, schedule, forecast, job and module views |
| GET | `/api/export?date=YYYY-MM-DD` | Basic-authenticated daily/forecast JSON download |
| GET | `/api/docs?path=docs/live_wiring.md` | Basic-authenticated allowlisted local documentation |
| POST | `/api/jobs` | Start a fixed action with `{"action":"demo-dca"}`, Basic auth and `X-Console-Token` |
| Any | Any route except `/api/health` | Missing or incorrect Basic auth returns JSON `401` and `WWW-Authenticate: Basic realm="DCA console", charset="UTF-8"` |
| Any | Any route | A rejected Host, Origin or Fetch Metadata check returns JSON `403` with bounded `received_host` and `forwarded_host`; a bad job token also returns `403` before the job is accepted |

Basic auth is required only when an auth file is configured. After a launchd
restart rotates the console token, the page handles the first job `403` by
fetching `/api/bootstrap` and retrying exactly once. Requests cannot provide
shell commands, arbitrary file paths or broker actions. Cross-site requests stay
rejected on every `/api/*` route; the only exception is a side-effect-free
`GET /` document navigation returning from a Cloudflare Access login.

## Remote access

The console does not schedule or submit live brokerage orders. A loopback-only listener needs no credential;
every non-loopback bind and every `--trusted-host` value requires `--auth-file`. The server honors a configured
`X-Forwarded-Host` only with `--behind-proxy`, and that mode requires a loopback
bind. Never use `tailscale funnel` and never expose a bare port-forward. The
launchd service always keeps the default loopback bind. A wildcard
`0.0.0.0` bind adds no accepted Host of its own and also requires at least one
trusted host.

The auth file is a POSIX-only, current-user-owned regular file with no group or
other permissions, at most 4096 bytes, containing one UTF-8
`user:password` line. The username must be printable ASCII without `:` and the
password must have at least 12 characters. The console reads the checked inode
through one bounded descriptor; errors name the failed rule, never the file's
contents. Only its path enters argv or the plist—the credential does not enter
argv, the environment, job logs, bootstrap data, snapshots, summaries, error
bodies or rendered service definitions.

Create the disposable Phase A credential with a restrictive umask. Re-run the
same block whenever a rotation is called for:

```bash
AUTH=~/.config/all-stock-dca/console-auth
mkdir -p ~/.config/all-stock-dca
umask 077
printf 'dca:%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" > "$AUTH"
```

A hosted `--local` console started without `--date` opens on today (ET), even
after the service crosses midnight. An explicit `--date` pins a day;
the launchd renderer does not set one.

### Phase A: home LAN (foreground, plaintext)

This is plain HTTP on the home wifi, and the Basic credential crosses that wifi
on every 15-second refresh; use a disposable credential, rotate it before Phase
B, and never install this LAN bind as the service. Start it only in the
foreground, replacing the documentation-only address with this Mac's LAN IPv4
address:

```bash
python3 from.py --local --no-browser --bind 192.0.2.10 --auth-file "$AUTH"
```

The startup banner deliberately includes this line:

```text
WARNING: plain HTTP: the Basic credential and console token are visible to anyone on this network
```

Stop the process with Ctrl+C when the phone test is complete. Once Tailscale is
installed, binding its interface IPv4 instead of the LAN IPv4 keeps traffic
inside the encrypted tailnet and avoids cleartext transport entirely; it
remains a non-loopback bind and still requires `--auth-file`.

### Phase B: Tailscale Serve

Proxy releases can change header behavior, so use the backend's diagnostic
health body rather than assuming what the installed version preserves. With the
console listening on loopback, enable Serve and confirm that Funnel is absent:

```bash
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

From another tailnet device, run:

```bash
curl -si https://mac.tailnet.ts.net/api/health
```

The JSON body, whether its status is `200` or `403`, shows the public name under
`received_host` or `forwarded_host`. If `mac.tailnet.ts.net` is under
`received_host`, do not add `--behind-proxy`. Add it only if that name is under
`forwarded_host` while `received_host` is something else. Stop and investigate
if neither field carries the configured name.

Rotate the Phase A credential by rerunning the generator above, then render the
service with the observed authority:

```bash
.venv/bin/python deploy/launchd/render_console_agent.py \
  --auth-file "$AUTH" \
  --trusted-host mac.tailnet.ts.net \
  --install
```

Only for the `forwarded_host` case, add `--behind-proxy` to that renderer
command. Apply the changed plist with `bootout` followed by `bootstrap`:

```bash
launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist
```

A changed plist is never applied with `kickstart`; that only restarts the
already-loaded definition. Repeat the health probe and expect `200`, then open
`https://mac.tailnet.ts.net/`. Do not enable Funnel.

### Phase B: Cloudflare Tunnel + Access

Before Access is in the path, check what the loopback backend does with the
planned name:

```bash
curl -si -H 'Host: console.example.com' http://127.0.0.1:8765/api/health
```

Before `--trusted-host` is installed, expect `403` with
`received_host: console.example.com`; after the service is re-rendered below,
the same local probe should return `200`. Create the tunnel, but create and
attach the Cloudflare Access application and policy for `console.example.com`
before publishing DNS. The name must never be public with Basic auth alone.

```bash
cloudflared tunnel create dca-console
```

Create and attach the Access application and policy now. Only after that policy
protects the hostname, publish DNS:

```bash
cloudflared tunnel route dns dca-console console.example.com
```

Use a `config.yml` whose console ingress targets loopback and whose final rule is
the mandatory catch-all:

```yaml
tunnel: <tunnel-id>
credentials-file: <credentials-json>
ingress:
  - hostname: console.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Validate the ingress before installing the tunnel service:

```bash
cloudflared tunnel ingress validate
cloudflared service install
```

Probe through Access; the URL is the first argument and curl flags follow it:

```bash
cloudflared access curl https://console.example.com/api/health -si
```

A plain `curl` is answered at Cloudflare's edge with a `302` to the Access login
and never reaches the console. The Access-authenticated probe returns `200` or
`403` with `received_host` and `forwarded_host`. Add `--behind-proxy` only when
the public name appears under `forwarded_host` and `received_host` is something
else; if it is under `received_host`, do not add the flag.

Rotate the credential, then render with the Cloudflare name:

```bash
.venv/bin/python deploy/launchd/render_console_agent.py \
  --auth-file "$AUTH" \
  --trusted-host console.example.com \
  --install
launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist
```

Only for the `forwarded_host` case, add `--behind-proxy` before rendering.
Repeat both health probes and expect `200`. A browser sees two authentication
steps: Cloudflare Access first, then the console's Basic prompt. Basic remains
mandatory because this standard-library-only server cannot verify Cloudflare
Access's RS256 JWT.

### Run as a login service (launchd)

The checkout-only renderer validates the auth file and selected interpreter,
then either prints XML or installs the fixed
`com.all-stock-dca.console` agent:

```bash
.venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --print
.venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --install
```

Exactly one of `--print` or `--install` is required. Optional flags are
`--python PATH`, `--port PORT`, repeatable `--trusted-host HOST[:PORT]`,
`--behind-proxy`, `--octopus-run DIR`, and `--cron-tracking-root DIR`. The
renderer has no `--bind` or `--label`; its installed console is always local
mode, loopback-only and browserless. The default interpreter is
`<checkout>/.venv/bin/python`, and rendering fails if the selected interpreter
does not exist. Pass `--octopus-run` if the hosted console should show forecasts.
`--print` only emits the plist; `--install` also creates the log directory and
prints operating commands.

The renderer resolves one artifact root with precedence
`--cron-tracking-root`, then `DCA_CRON_TRACKING_ROOT`, then the `dca_paths`
default. That same root feeds the console and both logs:
`<root>/console/launchd.out.log` and `<root>/console/launchd.err.log`.

The four launchctl operations are:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist
launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state|pid'
launchctl kickstart -k gui/$(id -u)/com.all-stock-dca.console
launchctl bootout gui/$(id -u)/com.all-stock-dca.console
```

Use `kickstart -k` only to restart an unchanged definition, such as after
rotating the contents of the same auth file. Whenever the plist changes, apply
it with this order instead:

```bash
launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist
```

The Mac must not sleep while it is expected to serve or refresh the console. If
`launchd.err.log` reports `Operation not permitted` while reading a checkout
under `~/Documents`, macOS TCC is blocking that interpreter. Grant the
interpreter Files and Folders or Full Disk Access in System Settings, or keep
the checkout outside `~/Documents`.

### Operator acceptance checklist

Common first step: rotate the credential file (regenerate it with the generator block above; the
Phase A value crossed the LAN in plaintext).

**Tailscale Serve**

1. Install Tailscale on this Mac and the phone; log in (operator step). `tailscale serve
   --bg http://127.0.0.1:8765`; `tailscale serve status` shows the tailnet URL and no Funnel.
2. Probe from a tailnet device (laptop on the tailnet, or the phone):
   `curl -si https://<mac>.<tailnet>.ts.net/api/health`. The JSON body (200 or 403) always
   carries `received_host` and `forwarded_host`. Read it: if the name appears under
   `received_host`, no `--behind-proxy` is needed; only if it appears under
   `forwarded_host` and `received_host` is something else, add `--behind-proxy`.
3. Re-render: `render_console_agent.py --auth-file … --trusted-host <mac>.<tailnet>.ts.net
   [--behind-proxy] --install`; run the printed apply pair (`bootout`, then `bootstrap`);
   `launchctl print` shows `state = running`. Repeat the probe: expect 200.
4. Second-device acceptance: phone on cellular, wifi off, Tailscale on:
   `https://<mac>.<tailnet>.ts.net/` prompts once, shows `LOCAL` mode with the latest
   scheduled runs, "Export JSON" downloads, a job button starts a job.

**Cloudflare Tunnel + Access**

1. Install cloudflared on this Mac; log in (operator step). Local pre-check of the Host
   semantics: `curl -si -H 'Host: console.<domain>' http://127.0.0.1:8765/api/health` →
   403 with `received_host: console.<domain>`.
2. `cloudflared tunnel create dca-console`; create the Access application and policy for
   `console.<domain>` **before** `cloudflared tunnel route dns`; `config.yml` ingress to
   `http://127.0.0.1:8765` plus the terminal `- service: http_status:404`;
   `cloudflared tunnel ingress validate`; `cloudflared service install`.
3. Probe through Access: `cloudflared access curl https://console.<domain>/api/health -si`
   (a plain `curl` is answered at the edge). The JSON body (200 or 403) carries
   `received_host` / `forwarded_host`; choose `--behind-proxy` by the same rule as above.
4. Re-render with `--trusted-host console.<domain> [--behind-proxy] --install`; run the
   printed apply pair; `launchctl print` shows `state = running`. Repeat the probe: expect 200.
5. Second-device acceptance: phone on cellular, wifi off: `https://console.<domain>/`
   completes the Access login, then prompts for Basic once, shows `LOCAL` mode with the
   latest scheduled runs, "Export JSON" downloads, a job button starts a job. Confirm the
   Access policy is attached to the hostname before leaving it running.

Implementation: `from.py` → `dca_console.py`, with separate daily-data,
Octopus-artifact, and job modules plus browser assets under `console/` and the
checkout-only launchd renderer under `deploy/launchd/`.

## Verification for Rock 5

```bash
CONSOLE_TESTS=(tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py)
# Adjacent shell strings preserve the retired sentence as a grep pattern without making this proof match itself.
git add -N deploy/launchd tests/test_console_launchd.py DEFERRED.md \
&& .venv/bin/python -m ruff check . && .venv/bin/python -m pytest "${CONSOLE_TESTS[@]}" tests/test_console_launchd.py tests/test_config_invariants.py scripts/tests/test_agent_entry_points.py -q \
&& ! grep -q 'Do not expose it through a public reverse ''proxy' docs/local_console.md \
&& grep -q 'does not schedule or submit live brokerage orders' docs/local_console.md \
&& grep -q 'http_status:404' docs/local_console.md && grep -q 'ingress validate' docs/local_console.md \
&& grep -q 'cloudflared access curl' docs/local_console.md && grep -q 'launchctl bootout' docs/local_console.md \
&& grep -q 'local_console.md' CLAUDE.md && grep -q 'from.py' docs/repo_map.md && test -f DEFERRED.md && echo DOCS-OK
```

The whole-repository coverage gate is separate and takes about five minutes:

```bash
.venv/bin/python -m pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90
```

`CONSOLE_TESTS` is a zsh/bash array and must be expanded as
`"${CONSOLE_TESTS[@]}"`; zsh does not split a scalar test list.
