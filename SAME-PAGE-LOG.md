# Same Page Meeting log — rocket-fuel/hosted-console

Visionary: Claude (Fable 5.1). Integrator: Codex CLI 0.145.0, `codex exec --sandbox read-only --json`.
Subject: PLAN.md (with VTO.md, DEFERRED.md). Max 5 rounds; verdict line is the literal last line of each Codex reply.

## Round 1 — plan as submitted

Timestamp: 2026-09-13T04:32:08Z
PLAN.md sha256: 315868e14c3b4eb9…  VTO.md sha256: f876df743cc468ea…

### Prompt sent

> You are the Integrator reviewing a plan written by the Visionary. Read PLAN.md and VTO.md
> end to end (DEFERRED.md lists what is intentionally out of scope). You are read-only — do
> not propose file edits, propose plan changes instead.
> 
> Ground your findings in the actual code: read dca_console.py (especially make_server,
> _trusted, do_GET, do_POST, main), dca_console_jobs.py (redact, JobManager, _command
> env handling), tests/test_dca_console.py (fixtures and the existing Host/Origin/token
> tests), docs/local_console.md, deploy/run-drift-check.sh, pyproject.toml (the
> [tool.ruff] / [tool.coverage] / [tool.setuptools] stanzas), and CLAUDE.md's "Hard
> invariants". Repo rules that bind this plan: standard-library-only runtime; fail closed;
> the console never places brokerage orders; no 6-12 digit literal in any tracked file
> (tests/test_config_invariants.py scans every git ls-files entry); no hard-coded absolute
> paths; heavy "why" comments.
> 
> Attack the plan for gaps, contradictions, scope creep, blind spots, and anything that
> would fail CI or violate a repo invariant. For every real problem you find, emit exactly
> one line in this format:
> 
> [SEVERITY] short-title — one sentence on why it matters
> 
> Severity is one of BLOCKER, MAJOR, MINOR, NIT. After the findings, for each one state a
> verdict: FIX (plan must change before anyone builds), DEFER (valid, but out of scope for
> this cycle), KILL (this scope should be cut entirely), or ACCEPT (flagged, but on
> reflection it's fine — explain why). End your reply with exactly one line, and make it
> the last line:
> `VERDICT: SAME PAGE` if you have zero open FIX or KILL items, otherwise
> `VERDICT: BLOCKED — N open finding(s)`.

### PLAN.md as reviewed

````markdown
# PLAN — Host the existing DCA console as a private web app

Build plan for the `rocket-fuel/hosted-console` cycle. Vision and non-negotiables live in
`VTO.md`; deferred items in `DEFERRED.md`. This plan is the subject of the Same Page
Meeting and is revised on the record until `VERDICT: SAME PAGE`.

## Starting point (facts, verified 2026-09-13)

- `dca_console.py` (502 lines): `make_server()` at line 317 builds
  `ThreadingHTTPServer(("127.0.0.1", port), Handler)` (line 412). `_trusted()` (339-345)
  accepts only `Host` in `{127.0.0.1:<port>, localhost:<port>}`, `Origin` matching
  `http://<those>`, and rejects `Sec-Fetch-Site: cross-site`. `main()` (415-498) has
  `--demo/--local/--root/--output-dir/--octopus-run/--research-python/--date/--port/
  --no-browser/--once/--run/--check`. POST `/api/jobs` requires `X-Console-Token`
  (`secrets.compare_digest`, line 390); GET `/api/bootstrap` hands the token out (357).
  Only `KeyboardInterrupt` is handled around `serve_forever` (492-497).
- `dca_console_jobs.py`: `redact()` (21-32) masks env values whose names contain
  `SECRET|TOKEN|PASSWORD|API_KEY|ACCOUNT_NUMBER`, labelled secrets, and any 6-12 digit run.
  `_command()` in `dca_console.py` (236-247) copies `dict(os.environ)` into every job child.
- Job history is bounded (30 jobs, 24 000 log chars); the snapshot cache clears at 32
  entries; `ConsoleApp.signatures` grows one key per day, unbounded.
- Tests: `tests/test_dca_console.py` (fixtures `app`, `http_server` via
  `make_server(app, 0)`, helpers `request()`/`post()`), plus `test_dca_console_data.py`,
  `test_dca_console_jobs.py`, `test_dca_console_octopus.py`,
  `test_console_security_logging.py` — 78 passing.
- Console modules are deliberately absent from `pyproject.toml` `py-modules` (lines
  140-142): checkout-only. `deploy/` is not shipped, but `deploy/*.py` **is** linted by
  ruff and counted by coverage (`deploy` is in neither exclude list).
- The live artifact tree is `<checkout>/cron_tracking` on this Mac
  (`dca_paths.cron_tracking_root()` with `DCA_CRON_TRACKING_ROOT` unset). No launchd
  precedent in the repo; `deploy/run-drift-check.sh` explains why service logic lives in
  a script rather than inline in a unit.
- `docs/local_console.md` lines 149-153 say the server "binds only to loopback" and
  "Do not expose it through a public reverse proxy." Line 94: "The console does not
  schedule or submit live brokerage orders."

## Phases

- **Phase A (build/test):** `--bind <LAN-IP> --auth-file <path>` on the home wifi.
- **Phase B (deploy):** default loopback bind + `--auth-file … --trusted-host <name>
  --behind-proxy`, fronted by Tailscale Serve **or** Cloudflare Tunnel + Access. Same code
  path; only the runbook differs.
- Run as a launchd login agent on this Mac in `--local --no-browser` mode.

## Design decisions

| Topic | Decision | Why |
|---|---|---|
| Secret source | `--auth-file PATH`: one line `user:password`; regular file, owned by the invoking user, mode `0600` (mode check skipped on Windows, matching the tolerance at `dca_console.py:439`); password ≥ 12 chars | An env var would be copied into every job child by `env = dict(os.environ)` (`dca_console.py:237`), show in `launchctl print` / `ps -E`, and sit inside the plist. A path in argv leaks nothing |
| Auth scheme | HTTP Basic on every route except `/api/health`, only when `--auth-file` is given; `secrets.compare_digest` on `b"user:password"`; failure → `401` + `WWW-Authenticate: Basic realm="DCA console", charset="UTF-8"` + JSON error | Native browser prompt on a phone; zero frontend change because same-origin `fetch` (`console/app.js:381,384,411`) and the `<a download>` export (`console/index.html:38`) attach credentials automatically |
| `/api/health` unauthenticated | Still behind the Host/Origin check; body is only `{"status","mode"}` | Lets probes and the launchd runbook work without embedding the password anywhere |
| `X-Console-Token` | Keep unchanged | Basic credentials are ambient like cookies; the token (fetched same-origin, no CORS headers) remains the CSRF defence |
| `--bind ADDR` | IPv4 literal only (`ipaddress.IPv4Address`), default `127.0.0.1` | `HTTPServer` is AF_INET; hostnames/IPv6 deferred |
| `--trusted-host HOST[:PORT]` (repeatable) | lowercased; hostname regex; no scheme, path, wildcard or whitespace; port ≤ 65535 | Extra names accepted in `Host`. Also accept `https://` origins (today only `http://`, `dca_console.py:344`, which would block POSTs behind an HTTPS proxy) |
| `hosts(port)` | `{127.0.0.1:port, localhost:port}` ∪ `{bind:port}` when bind is a specific non-loopback IP ∪ `trusted_hosts`; `0.0.0.0` adds nothing | Phase A needs no `--trusted-host`; with `0.0.0.0` the server cannot know which address the client typed |
| `--behind-proxy` | Additionally accept a **configured** name (never a loopback name) from `X-Forwarded-Host`; requires ≥1 `--trusted-host` **and** a loopback bind | A LAN-reachable socket that honours forwarded headers would let any LAN client forge them |
| Fail-closed matrix (`parser.error`, exit 2, before `ConsoleApp` is built) | non-loopback bind without `--auth-file`; any `--trusted-host` without `--auth-file`; `--behind-proxy` without `--trusted-host` or with a non-loopback bind; `--bind 0.0.0.0` without `--trusted-host`; malformed `--bind` / `--trusted-host` values | Nothing listens on a reachable address without a credential |
| `Sec-Fetch-Site: cross-site` | Still rejected for POST and for any GET that is not a top-level navigation (`Sec-Fetch-Mode: navigate` + `Sec-Fetch-Dest: document`) | Cloudflare Access login is a redirect chain through `<team>.cloudflareaccess.com`; Fetch Metadata computes the site over the whole chain, so the first load after login arrives `cross-site`. A bare page load is harmless (CSP `frame-ancestors 'none'`, no CORS); cross-site `fetch`/POST stay blocked. Existing test at `tests/test_dca_console.py:122-124` still gets 403 |
| 403 body | adds `received_host` (≤ 253 chars) and `forwarded_host` | The runbook's "what does the backend see" probe; JSON with `nosniff`, no HTML |
| Brute-force backoff | Deferred (see `DEFERRED.md`); 12-char minimum, documented generator, one stderr line per failure (client IP + path, never credentials) | Behind either proxy every request is from `127.0.0.1`; per-IP state becomes a global lockout lever |
| SIGTERM | Context manager installs a handler that raises `KeyboardInterrupt`, reusing the Ctrl+C teardown (`dca_console.py:492-497`); restores the previous handler; wraps only the serving branch | `server.shutdown()` from the handler would deadlock (same thread as `serve_forever`). Worst-case teardown ≈ 11 s (`dca_console_jobs.py:210-226`), inside the plist's `ExitTimeOut = 30` |
| Handler hygiene | `Handler.timeout = HANDLER_TIMEOUT_SECONDS` (30; today `None`, only POST bodies get `settimeout(5)` at line 400); `version_string()` override | An idle LAN/proxy connection must not pin a thread; `Server:` should not advertise the Python version |
| Long uptime | Cap `ConsoleApp.signatures` to the last 32 days | Everything else is already bounded |
| launchd | `deploy/launchd/render_console_agent.py` renders the plist with `plistlib` (`--print` / `--install`), validates the auth file via `load_auth_file`, refuses without `<checkout>/.venv/bin/python`, computes log paths from `stream_root("console")`, creates the log directory, prints the `launchctl` commands. No placeholder template | Same "logic in a script, not the unit" rationale as `deploy/run-drift-check.sh:6-17`; `plistlib` cannot mis-escape; `stream_root` honours `DCA_CRON_TRACKING_ROOT` |
| pyproject | No change | Console is checkout-only; `deploy/` is not shipped |
| Proxy `Host` semantics (read from source at HEAD, not a live install — uncertainty acknowledged) | Tailscale Serve preserves `Host` and adds `X-Forwarded-Host/-Proto/-For` + `Tailscale-User-*`; cloudflared preserves `Host` unless `httpHostHeader` is set | Accept the configured name in `Host` or (with `--behind-proxy`) `X-Forwarded-Host`; every runbook starts with a `curl -si …/api/health` whose 403 body prints what arrived |

## Rocks

Sequencing: 1 → 2 → 3; 4 after 1 (parallel with 3); 5 last.

`CONSOLE_TESTS` = `tests/test_dca_console.py tests/test_dca_console_jobs.py
tests/test_dca_console_data.py tests/test_dca_console_octopus.py
tests/test_console_security_logging.py` (78 today). Credential file created once:

```bash
mkdir -p ~/.config/all-stock-dca && umask 077 && printf 'dca:%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" > ~/.config/all-stock-dca/console-auth
```

Fixture passwords and hosts are letters-and-hyphens (`s3cret-pw-abcdefg`, `192.0.2.10`,
`mac.tailnet.ts.net`) so the account-shaped literal scan stays quiet.

### Rock 1 — Basic auth from a 0600 file (loopback-safe, no exposure yet)

**Scope.** Frozen `ConsoleAccess` dataclass beside `Settings` (`username: str`,
`credential: bytes | None = field(default=None, repr=False)`); `load_auth_file(path) ->
tuple[str, bytes]` with the rules above, error messages naming the rule but never the
contents; `ConsoleAccess.authorized(headers)` (`Basic <b64>`, scheme case-insensitive,
`base64.b64decode(..., validate=True)`, `compare_digest`); `--auth-file`; 401 path on every
route except `/api/health` — order `trusted` → 403, `authorized` → 401, then token → 403 on
POST; `dca_console_jobs.register_secret()` consulted by `redact()`; one stderr line per
failure. `make_server(app, port, access=None)` keeps today's behaviour by default so all
78 existing tests pass untouched. Frontend untouched.

**Non-goals.** `--bind`, trusted hosts, proxy headers (Rock 2); backoff; UI hint that
auth is on.

**Files.** `dca_console.py`, `dca_console_jobs.py`, `tests/test_dca_console.py`.

**Tests.** `test_auth_file_rules_fail_closed` (missing, mode 0644, no colon, empty, two
lines, 11-char password → `ValueError` without file contents; good file →
`("dca", b"dca:s3cret-pw-abcdefg")`); `test_basic_auth_required_on_every_route_except_health`
(every asset, `/api/bootstrap`, `/api/snapshot`, `/api/export`, `/api/docs` → 401 with
`WWW-Authenticate` starting `Basic realm=`; `/api/health` → 200; POST with right token but
no credentials → 401 and `app.submit` never called);
`test_basic_auth_wrong_credentials_are_rejected` (wrong password, wrong user, undecodable
base64, `Bearer` scheme → 401);
`test_basic_auth_correct_credentials_pass_and_token_still_required` (bootstrap 200; POST
with credentials but no token → 403; with both → 202);
`test_password_never_appears_in_responses_or_redaction` (bodies/headers lack it; `redact()`
masks it; `repr(access)` lacks it); `test_auth_failure_is_logged_without_credentials`.

**Proof.**
```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_config_invariants.py -q \
&& (.venv/bin/python from.py --local --no-browser --port 8765 --auth-file ~/.config/all-stock-dca/console-auth & sleep 4; \
    curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/health; \
    curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/bootstrap; \
    curl -s -o /dev/null -w '%{http_code}\n' -u "$(cat ~/.config/all-stock-dca/console-auth)" http://127.0.0.1:8765/api/bootstrap; kill %1)
```
Expected: ruff clean; 84 console passes (78 + 6) and the invariants file green; curl
prints `200 401 200`.

### Rock 2 — `--bind`, `--trusted-host`, `--behind-proxy`, hardened handler (Phase A reachable)

**Scope.** Extend `ConsoleAccess` with `bind`, `trusted_hosts`, `behind_proxy`; pure
`hosts(port)` and `trusted(headers, port)`; replace `_trusted()` (`dca_console.py:339-345`);
`https://` origins; navigation-only cross-site tolerance; 403 diagnostic body;
`ThreadingHTTPServer((access.bind, port), …)` (line 412); the fail-closed matrix;
`HANDLER_TIMEOUT_SECONDS = 30`; `version_string()`; banner (472-473) prints bind, port,
trusted hosts and `auth: basic user=<name>` (never the password); reword the `--port` help
(426) and the bind-failure message (470); `webbrowser.open` targets the bind IP when it is
a specific address.

**Non-goals.** IPv6 / hostname binds; in-process TLS; proxy identity headers; UI changes.

**Files.** `dca_console.py`, `tests/test_dca_console.py`.

**Tests.** `test_exposure_flags_fail_closed` (`main([...])` → `SystemExit` code 2 for the
whole matrix plus `--bind not-an-ip`, `--bind ::1`, `--trusted-host` values `http://x`,
`x/y`, `x:99999`, `x y`, `*.ts.net`);
`test_trusted_hosts_accept_configured_names_and_https_origin` (`Host: mac.tailnet.ts.net`
+ `Origin: https://mac.tailnet.ts.net` + credentials → 200; upper-case Host → 200;
`Host: other.ts.net` → 403 with `received_host == "other.ts.net"`; loopback Host still 200);
`test_forwarded_host_is_ignored_unless_behind_proxy` (`Host: untrusted.example` +
`X-Forwarded-Host: mac.tailnet.ts.net` → 403 on a non-proxy server, 200 with
`behind_proxy=True`; `X-Forwarded-Host: 127.0.0.1:<port>` on the proxy server → 403);
`test_hosts_for_bind_addresses` (pure); `test_cross_site_navigation_allowed_but_cross_site_fetch_and_post_rejected`;
`test_idle_connection_is_closed_by_handler_timeout` (`HANDLER_TIMEOUT_SECONDS = 0.2`, raw
socket that sends nothing gets EOF within 2 s); `test_server_header_does_not_leak_python_version`.

**Proof.**
```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_config_invariants.py -q \
&& LAN=$(ipconfig getifaddr en0) && (.venv/bin/python from.py --local --no-browser --bind "$LAN" --auth-file ~/.config/all-stock-dca/console-auth & sleep 4; \
    curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/health"; \
    curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/snapshot"; \
    curl -s -o /dev/null -w '%{http_code} ' -u "$(cat ~/.config/all-stock-dca/console-auth)" "http://$LAN:8765/api/snapshot"; \
    curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' "http://$LAN:8765/api/health"; kill %1) \
&& { .venv/bin/python from.py --local --no-browser --bind 0.0.0.0; echo "exit=$?"; }
```
Expected: ruff clean; 91 console passes (84 + 7); curl prints `200 401 200 403`; the last
command prints the fail-closed message and `exit=2`. Then a phone on the same wifi:
`http://<LAN>:8765` prompts once, shows `LOCAL` mode with real runs, and "Export JSON"
downloads.

### Rock 3 — Clean SIGTERM and long-uptime bounds

**Scope.** `_sigterm_as_keyboard_interrupt()` context manager around `serve_forever`
(line 491), restoring the previous handler, wrapping only the serving branch; cap
`signatures` (129, 225) to the last 32 days.

**Non-goals.** SIGHUP reload; job-file pruning; log rotation.

**Files.** `dca_console.py`, `tests/test_dca_console.py`.

**Tests.** `test_sigterm_stops_console_cleanly` (posix-only subprocess: spawn
`from.py --local --no-browser --port 0 --root <tmp> --output-dir <tmp>`, wait for the
`DCA console (local)` banner, `send_signal(SIGTERM)`, `wait(timeout=20)` → returncode 0
and stdout contains `Stopping local console.`);
`test_sigterm_handler_raises_and_restores_previous_handler`;
`test_summary_signature_cache_is_bounded` (100 distinct days → `len(app.signatures) <= 32`).

**Proof.**
```bash
.venv/bin/python -m pytest $CONSOLE_TESTS -q \
&& (.venv/bin/python from.py --local --no-browser --port 8765 --auth-file ~/.config/all-stock-dca/console-auth & sleep 4; kill -TERM %1; wait %1; echo "exit=$?")
```
Expected: 94 passes; output ends with `Stopping local console.` then `exit=0` within seconds.

### Rock 4 — launchd agent renderer

**Scope.** `deploy/launchd/render_console_agent.py` (stdlib `argparse`, `plistlib`, `os`,
`pathlib`; `sys.path.insert(1, <checkout>)` per the ticker-advisor precedent, then
`from dca_paths import stream_root` and `from dca_console import load_auth_file`). Args:
`--auth-file` (required), `--port 8765`, `--trusted-host` (repeatable), `--behind-proxy`,
`--label com.all-stock-dca.console`, optional `--cron-tracking-root` (→
`EnvironmentVariables`), exactly one of `--print` / `--install`. Rendered dict:

```
Label                = com.all-stock-dca.console
ProgramArguments     = [<checkout>/.venv/bin/python, <checkout>/from.py, --local, --no-browser,
                        --port, 8765, --auth-file, <abs path>, (--trusted-host, H)*, (--behind-proxy)]
WorkingDirectory     = <checkout>
RunAtLoad            = true
KeepAlive            = { SuccessfulExit = false }   # restart on failure; a clean SIGTERM exit (main returns 0) is not restarted
ThrottleInterval     = 15                            # a persistent bind failure (main returns 1) retries every 15 s, no hot loop
ExitTimeOut          = 30
ProcessType          = Background
StandardOutPath      = <stream_root('console')>/launchd.out.log
StandardErrorPath    = <stream_root('console')>/launchd.err.log
EnvironmentVariables = { PYTHONUNBUFFERED = "1", PATH = "/usr/bin:/bin:/usr/sbin:/sbin", [DCA_CRON_TRACKING_ROOT = …] }
```

No secret anywhere in the plist. `--install` writes `~/Library/LaunchAgents/<label>.plist`
(0644), creates the log directory (launchd creates log files but not their parent), and
prints:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist
launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state|pid'
launchctl kickstart -k gui/$(id -u)/com.all-stock-dca.console
launchctl bootout gui/$(id -u)/com.all-stock-dca.console
```

`.gitignore` gains `deploy/launchd/*.plist` beside the existing `deploy/*.env` line.

**Non-goals.** Running `launchctl` from the script; a `/Library/LaunchDaemons` variant;
installing Tailscale or cloudflared.

**Files.** `deploy/launchd/render_console_agent.py` (new), `tests/test_console_launchd.py`
(new; imports the script via `importlib.util.spec_from_file_location` so coverage counts
it), `.gitignore`.

**Tests.** `test_renders_expected_program_arguments_and_paths` (Phase A and Phase B forms;
`plistlib.loads` output has the exact `ProgramArguments`, `KeepAlive ==
{"SuccessfulExit": False}`, `RunAtLoad`, `ExitTimeOut == 30`, log paths under
`stream_root("console")`, no `EnvironmentVariables` key containing `PASSWORD`);
`test_rendered_plist_contains_no_secret`; `test_renderer_fails_closed` (loose-mode auth
file, `--behind-proxy` without `--trusted-host`, missing venv python → exit 2);
`test_install_writes_agent_and_creates_log_dir` (`HOME` → tmp; 0644; log dir exists).

**Proof.**
```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_console_launchd.py tests/test_config_invariants.py -q \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file ~/.config/all-stock-dca/console-auth --print | plutil -lint - \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file ~/.config/all-stock-dca/console-auth --install \
&& launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist && sleep 5 \
&& curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/health \
&& launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state = |pid = '
```
Expected: tests green (94 console + 4 launchd); `stdin: OK`; `200`; `state = running`
with a pid. Afterwards `launchctl bootout gui/$(id -u)/com.all-stock-dca.console` and
`tail -2 cron_tracking/console/launchd.out.log` shows `Stopping local console.`

### Rock 5 — Docs, runbooks, DEFERRED.md

**Scope.** `docs/local_console.md`: replace lines 149-153 with a **Remote access** section
that opens with "The console does not schedule or submit live brokerage orders" and states
the precise rule (loopback needs nothing; any non-loopback bind or external name requires
`--auth-file`; forwarded headers only with `--behind-proxy` on a loopback bind; never
`tailscale funnel`, never a bare port-forward). Sub-sections: **Phase A: home LAN**;
**Phase B: Tailscale Serve** (`tailscale serve --bg http://127.0.0.1:8765`, confirm with
`tailscale serve status`); **Phase B: Cloudflare Tunnel + Access** (`tunnel create` →
Access application + policy **before** `tunnel route dns`, so the name is never public with
Basic alone → `config.yml` ingress to `http://127.0.0.1:8765` → `cloudflared service
install`; note the expected double prompt and why Basic stays: stdlib cannot verify the
Access RS256 JWT); **Run as a login service (launchd)**. Each runbook starts with the
header-echo `curl -si …/api/health` and ends with re-rendering the plist with
`--trusted-host <name> --behind-proxy` plus `launchctl kickstart -k`. Add the new flags to
the entry-points block (102-129), 401/403 semantics to the API table (138-147), replace
the stale "Verification for this change" (158-178) with the Rock proofs. Note that a
network-mounted `--root` makes the 15-second `rglob` the hot spot and that the Mac must not
sleep. `CLAUDE.md`: architecture row "Local console" (`from.py` → `dca_console.py`,
`dca_console_{data,jobs,octopus}.py`, `console/`, `deploy/launchd/`; checkout-only, never
places orders), doc-index row for `docs/local_console.md`, commands block gains
`python3 from.py --local --no-browser`. `docs/repo_map.md` §1.7 row + §4 entry "see
today's runs from my phone". `docs/README.md` row for `local_console.md` extended.
`MEMORY.md` "Loopback UI" → "loopback by default; LAN/proxy with `--auth-file`".
`DEFERRED.md` kept current.

**Non-goals.** `HANDOFF.md` (historical record); `scripts/agent_entry_points.py` (its
`from.py` entry is flag-agnostic).

**Files.** `docs/local_console.md`, `CLAUDE.md`, `docs/repo_map.md`, `docs/README.md`,
`MEMORY.md`, `DEFERRED.md`.

**Proof.**
```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_console_launchd.py tests/test_config_invariants.py scripts/tests/test_agent_entry_points.py -q \
&& ! grep -q 'Do not expose it through a public reverse proxy' docs/local_console.md \
&& grep -q 'does not schedule or submit live brokerage orders' docs/local_console.md \
&& grep -q 'local_console.md' CLAUDE.md && grep -q 'from.py' docs/repo_map.md && test -f DEFERRED.md && echo DOCS-OK
```

## Whole-cycle verification (before commit approval)

```bash
ruff check .
.venv/bin/python -m pytest $CONSOLE_TESTS tests/test_console_launchd.py tests/test_config_invariants.py -q
.venv/bin/python -m pytest            # full core suite (~5 min)
.venv/bin/python -m pytest scripts/tests -q
python run_dca.py --demo --report --now 2026-09-11T15:45 --artifacts-dir /tmp/dca-demo
```

Manual: Rock 2's LAN sequence from a phone; Rock 4's launchd bootstrap / health / bootout
cycle. Tailscale and Cloudflare steps are documented with the header-echo probe first, but
are exercised only once the operator installs and logs in (that step is theirs).
````

### Attempt 1 — failed before review (infrastructure, not plan)

`codex exec --sandbox read-only --json` returned `turn.failed` with HTTP 400:
"The 'gpt-6-astra' model requires a newer version of Codex." The operator's
`~/.codex/config.toml` pins `model = "gpt-6-astra"`, which CLI 0.145.0 cannot drive.
Per the skill's retry rule, retried **once** with a per-invocation override
(`codex exec -m gpt-5.6-sol …`, a model present in both the account's models cache and
the CLI binary). The config file was not modified. Stdin redirected from `/dev/null` so a
non-interactive run never blocks on "Reading additional input from stdin".

### Attempt 2 — Codex reply (session `01a0990a-6696-75a1-bd9c-f80077cbea0f`, model override `gpt-5.6-sol`)

Timestamp: 2026-09-13T05:10:20Z

> I’ll audit the Visionary’s plan against the named implementation, tests, docs, CI configuration, and hard invariants, then report only actionable plan-level findings in the requested format. I’ll keep the workspace read-only.
> The first concrete contradiction is confirmed: the proposed curl proofs place the Basic credential in the process argument list, violating the plan’s own credential invariant. I’m now checking boundary enforcement and deployment/CI assumptions so the final list distinguishes true build blockers from deferred risks.
> [BLOCKER] credential-in-curl-argv — Rock 1 and Rock 2 expand `console-auth` into `curl -u`, making `user:password` process-visible despite VTO’s absolute no-credential-in-argv rule.
> [BLOCKER] fail-closed-only-at-cli — The matrix exercises `main()`, but the plan does not require `make_server()` itself to reject credential-free non-loopback access before `ThreadingHTTPServer` opens a socket.
> [MAJOR] plaintext-lan-basic-auth — Phase A carries the reusable Basic secret, CSRF token, and console data over LAN HTTP, where password length cannot prevent capture and replay ([Cloudflare guidance](https://developers.cloudflare.com/fundamentals/security/protect-your-origin-server/)).
> [MAJOR] cross-site-get-exception-writes — The exception covers every navigational GET even though `/api/snapshot` and `/api/export` call `_save_summary()`, so these cross-site requests are not the harmless page loads the plan claims.
> [MINOR] origin-not-bound-to-host — The plan never requires `Origin` to match the effective `Host` or `X-Forwarded-Host`, so the current weakness where both merely belong to allowed sets can survive the rewrite.
> [MAJOR] auth-file-confidentiality-unproved — Tests omit non-regular and wrong-owner files, while the Windows mode exemption supplies neither an equivalent ACL check nor a fail-closed platform restriction.
> [MAJOR] hosted-default-date-freezes — `main()` freezes `Settings.day` at startup and the frontend adopts that server default, so a launchd process surviving midnight opens new tabs on stale run data.
> [MAJOR] restart-breaks-job-buttons — `app.token` changes after a launchd restart while `app.js` bootstraps only when `state.token` is empty, leaving existing tabs unable to launch jobs until manually reloaded.
> [MAJOR] launchd-drops-octopus-input — VTO promises Wondrous Octopus forecasts, but the renderer emits no `--octopus-run` and `_load_demo_pointer()` is demo-only, so unattended local mode shows forecasts as unconfigured.
> [MAJOR] kickstart-does-not-reload-plist — Rock 5 rewrites `ProgramArguments` and then only runs `launchctl kickstart -k`, which restarts the already-loaded definition without applying the new proxy flags.
> [MAJOR] custom-root-splits-service-output — The child receives the root override through its plist environment while log paths use the renderer’s ambient `stream_root()`, allowing logs and console artifacts to land in different trees.
> [MINOR] custom-label-is-an-unsafe-target — Arbitrary `--label` text becomes both a plist path component and copy-pasted shell text without grammar or containment validation.
> [MAJOR] verification-omits-coverage-gate — `deploy` code counts toward coverage and CI enforces 90%, but every proof uses plain pytest, so all listed checks can pass while CI fails.
> [MINOR] focused-tests-variable-is-never-set — `CONSOLE_TESTS` is prose rather than a shell assignment, so fresh-shell proofs expand it to nothing and omit the console tests whose pass counts they claim.
> [MINOR] new-files-escape-local-literal-scan — The invariant reads `git ls-files`, so the new renderer and test remain invisible to local proofs until indexed even though CI will scan them after commit.
> [MAJOR] cloudflared-ingress-is-incomplete — Rock 5 does not require the mandatory terminal catch-all ingress rule, so its documented Cloudflare configuration can fail validation ([Cloudflare configuration docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)).
> [MINOR] renderer-cannot-produce-phase-a — Rock 4 claims Phase A and Phase B forms, but the renderer has no `--bind` argument and cannot emit the LAN-bound Phase A shape.
> [MAJOR] hosted-success-is-not-gated — VTO requires phone access over HTTPS, but whole-cycle verification postpones both proxy executions, allowing completion without demonstrating the cycle’s primary outcome.
> 
> credential-in-curl-argv — FIX — Replace authenticated curl proofs with a stdin, protected-config, or interactive mechanism that never materializes the secret in argv.
> fail-closed-only-at-cli — FIX — Require shared validation at `ConsoleAccess` construction or `make_server()` and directly test that invalid access never invokes the server constructor.
> plaintext-lan-basic-auth — FIX — Restrict direct HTTP to temporary synthetic testing with disposable credentials and require HTTPS before exposing real local data.
> cross-site-get-exception-writes — FIX — Limit the redirect exception to the initial document route and require API GETs to remain cross-site-rejected.
> origin-not-bound-to-host — FIX — Specify exact Origin-to-effective-authority matching and add mismatched-alias tests.
> auth-file-confidentiality-unproved — FIX — Add regular-file and ownership regressions and fail closed for remote hosting on platforms where privacy cannot be verified.
> hosted-default-date-freezes — FIX — Add rolling local-mode defaults and a midnight rollover regression.
> restart-breaks-job-buttons — FIX — Include frontend token re-bootstrap behavior and a restart regression without blindly retrying a possibly accepted job.
> launchd-drops-octopus-input — FIX — Add validated Octopus-path passthrough or narrow the VTO’s hosted feature promise.
> kickstart-does-not-reload-plist — FIX — Require `bootout` followed by `bootstrap` whenever the rendered plist changes.
> custom-root-splits-service-output — FIX — Resolve one canonical root and use it for both plist environment and log paths, with an exact custom-root test.
> custom-label-is-an-unsafe-target — KILL — Use the fixed launchd label this cycle and defer customizable labels.
> verification-omits-coverage-gate — FIX — Add CI’s exact coverage-gated pytest command to whole-cycle verification.
> focused-tests-variable-is-never-set — FIX — Define a shell-safe array or spell out the focused test paths in every proof.
> new-files-escape-local-literal-scan — FIX — Require indexing new files before the invariant proof or add an explicit scan including untracked nonignored files.
> cloudflared-ingress-is-incomplete — FIX — Specify the terminal catch-all and a `cloudflared` configuration-validation step.
> renderer-cannot-produce-phase-a — FIX — Declare Phase A manual and remove the renderer claim, or add validated bind passthrough.
> hosted-success-is-not-gated — FIX — Require one operator-assisted Phase B end-to-end acceptance before cycle completion or redefine success as code-and-runbook readiness.
> VERDICT: BLOCKED — 18 open finding(s)

### Round 1 — Discuss (Visionary responses, on the record)

| # | Finding | Codex | Visionary response |
|---|---|---|---|
| 1 | credential-in-curl-argv | FIX | **Agree.** Proofs now use `curl -K <(printf 'user = "%s"\n' "$(<"$AUTH")")`: the config arrives through a `/dev/fd` path, argv holds only the path. |
| 2 | fail-closed-only-at-cli | FIX | **Agree.** Validation moves into `ConsoleAccess.__post_init__` (credential shape in Rock 1; the bind/host matrix in Rock 2 once those fields exist). `main()` maps `ValueError` → `parser.error`. New test: `make_server` with an invalid access raises before `ThreadingHTTPServer` is constructed (constructor monkeypatched to fail the test if called). |
| 3 | plaintext-lan-basic-auth | FIX | **Agree on the risk; bounded rather than eliminated.** The operator explicitly chose LAN-first for build/test (interview gate 1). Bounds: Phase A is a foreground run and never the launchd service (the renderer cannot emit `--bind`, see #17); Phase A uses a disposable credential that the runbook rotates before Phase B; the runbook's first sentence says LAN HTTP is plaintext on the home wifi; in-process TLS stays in `DEFERRED.md` as the proper fix. Not adopting "synthetic data only": the operator's stated purpose for Phase A is testing against real runs. |
| 4 | cross-site-get-exception-writes | FIX | **Agree.** Exception narrowed to `GET /` only (the document route); every `/api/*` GET stays cross-site-rejected. The `_save_summary()` side effect of `/api/snapshot` is now stated in the plan. |
| 5 | origin-not-bound-to-host | FIX | **Agree.** When `Origin` is present it must equal `http://` or `https://` + the *effective authority* (the accepted `Host`, or the accepted `X-Forwarded-Host` under `--behind-proxy`). Test: loopback `Host` + `Origin: https://mac.tailnet.ts.net` → 403. |
| 6 | auth-file-confidentiality-unproved | FIX | **Agree.** `load_auth_file` requires POSIX (`os.name == "posix"`, otherwise raises — the Windows tolerance is dropped for this feature, fail closed), `S_ISREG` on `lstat` (rejects symlinks, FIFOs, directories), `st_uid == os.getuid()`. Tests: symlink, FIFO, directory, wrong owner (monkeypatched `os.getuid`), non-POSIX (monkeypatched `os.name`). |
| 7 | hosted-default-date-freezes | FIX | **Agree.** `ConsoleApp.default_day()` returns `datetime.now(ET).date()` in local mode and `settings.day` in demo mode; used by `/api/bootstrap`, the `snapshot()` default and the `default_date` field. Test: a monkeypatched clock crossing midnight changes `default_date` without a restart. |
| 8 | restart-breaks-job-buttons | FIX | **Agree.** `console/app.js`: on a 403 from `POST /api/jobs`, clear `state.token`, re-bootstrap once, retry once. Safe because `do_POST` returns 403 before `submit()`, so a 403 proves the job was never accepted; a second 403 surfaces the error. Lands in Rock 3 (now "restart resilience"). No JS test harness exists; proof is the manual `kickstart -k` step with an open tab. |
| 9 | launchd-drops-octopus-input | FIX | **Agree.** Renderer gains optional `--octopus-run DIR` (must be an existing directory) passed through to `from.py`; VTO narrowed: forecasts appear only when an Octopus run directory is configured. |
| 10 | kickstart-does-not-reload-plist | FIX | **Agree.** Runbook: any plist change → `launchctl bootout …` then `launchctl bootstrap …`; `kickstart -k` only restarts an unchanged definition (e.g. after rotating the auth file). |
| 11 | custom-root-splits-service-output | FIX | **Agree.** Renderer resolves one root (`--cron-tracking-root` or `dca_paths.cron_tracking_root()`), always emits `DCA_CRON_TRACKING_ROOT=<root>` in the plist environment, and derives log paths as `<root>/console/launchd.*.log` from that same value. Custom-root test asserts both agree. |
| 12 | custom-label-is-an-unsafe-target | KILL | **Agree.** `--label` removed; the label is the fixed `com.all-stock-dca.console`. Recorded in `DEFERRED.md`. |
| 13 | verification-omits-coverage-gate | FIX | **Agree.** Whole-cycle verification adds CI's exact command: `pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90`. |
| 14 | focused-tests-variable-is-never-set | FIX | **Agree.** Every proof block starts with an explicit `CONSOLE_TESTS="…"` assignment. |
| 15 | new-files-escape-local-literal-scan | FIX | **Agree.** Rock 4 and 5 proofs run `git add -N` on the new files before `tests/test_config_invariants.py`. |
| 16 | cloudflared-ingress-is-incomplete | FIX | **Agree.** `config.yml` ends with `- service: http_status:404`; `cloudflared tunnel ingress validate` runs before `cloudflared service install`. |
| 17 | renderer-cannot-produce-phase-a | FIX | **Agree (resolved with #3).** The renderer has no `--bind`; the launchd service is loopback-only by design; Rock 4 claims only the Phase B form; Phase A is a documented foreground command. |
| 18 | hosted-success-is-not-gated | FIX | **Agree, redefined on the record.** Installing and logging in to Tailscale or Cloudflare is the operator's step (account creation/login is outside the Visionary's reach). VTO success criterion 3 becomes: LAN acceptance demonstrated in-cycle, plus Phase B runbooks with an operator acceptance checklist. The plan states the cycle is *code-complete* at commit approval and *deployed* only when the operator completes that checklist. |

Solve: `PLAN.md`, `VTO.md` and `DEFERRED.md` revised accordingly (see the round-2 prompt below for the per-finding resolution list). The Visionary also ran an independent four-lens adversarial review of the same plan (Claude workflow `plan-pressure-test`); its confirmed findings are recorded before round 2 is sent.

