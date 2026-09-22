# Same Page Meeting log — rocket-fuel/hosted-console

Visionary: Claude (Fable 5.1). Integrator: Codex CLI 0.145.0, `codex exec --sandbox read-only --json`.
Subject: PLAN.md (with VTO.md, DEFERRED.md). Max 5 rounds; verdict line is the literal last line of each Codex reply.

## Round 1 — plan as submitted

Timestamp: 2026-09-13T04:32:08Z
PLAN.md sha256: 315868e14c3b4eb9…  VTO.md sha256: f876df743cc468ea…

### Prompt sent (Round 1)

> You are the Integrator reviewing a plan written by the Visionary. Read PLAN.md and VTO.md
> end to end (DEFERRED.md lists what is intentionally out of scope). You are read-only — do
> not propose file edits, propose plan changes instead.
>
> Ground your findings in the actual code: read dca_console.py (especially make_server,
> _trusted, do_GET, do_POST, main), dca_console_jobs.py (redact, JobManager,_command
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
| --- | --- | --- | --- |
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

### Round 1 (addendum) — Visionary's independent adversarial panel

Timestamp: 2026-09-13T05:15:27Z. Claude workflow `plan-pressure-test`: four lens reviewers
(security, repo invariants/CI, technical correctness, operability) over PLAN.md revision 1,
15 raw findings, each judged by three independent skeptics prompted to refute; a finding
survived only with ≥ 2 of 3 upholding it. 11 survived, 4 were refuted. Responses:

| # | Finding (lens, votes) | Overlaps Codex | Visionary response |
| --- | --- | --- | --- |
| C1 | Phase A cleartext credential every 15 s, carried unrotated into Phase B, proofs put it in argv (security, 2/3) | #1, #3 | **Already in rev 2** (foreground-only Phase A, disposable credential + rotation, plaintext stated, `curl -K <(…)`). **Added:** a startup banner line when the bind is non-loopback ("plain HTTP: the Basic credential and console token are visible to anyone on this network") via a pure `startup_banner()` with a test; runbook note that Phase A may bind the Tailscale interface IP instead (WireGuard-encrypted). |
| C2 | Navigation exemption covers side-effecting `/api/*` GETs (security, 3/3) | #4 | **Already in rev 2** (exemption only for `GET /`). **Added:** the test also asserts `app.snapshot` is not invoked for a cross-site navigational GET to `/api/snapshot`. |
| C3 | Auth-failure stderr line is an unauthenticated, unbounded, unredacted write path (security, 3/3) | — | **Agree, FIX (Rock 1).** Fixed shape: `redact()` of client address, method and `urlsplit(path).path[:128]` (route only, no query); rate-limited to one line per 5 s per process with a suppressed count. Test: 60 KB path and a path containing the fixture password → one short masked line; a burst of 50 failures → ≤ 2 lines. |
| C4 | Renderer tests exit 2 in CI because the workspace has no `.venv` (invariants, 3/3) | — | **Agree, FIX (Rock 4).** Renderer gains `--python PATH` (default `<checkout>/.venv/bin/python`, still fail-closed when the default is missing); happy-path tests pass `sys.executable` and assert `ProgramArguments[0]`; the missing-default case stays as the exit-2 test. Plan-level rule: no test may assume `<checkout>/.venv` exists. |
| C5 | `--cron-tracking-root` relocates artifacts but not log paths (invariants, 3/3) | #11 | **Already in rev 2** (one resolved `ROOT` for both). |
| C6 | Local-mode default day frozen at launch (correctness, 3/3) | #7 | **Already in rev 2** (`ConsoleApp.default_day()`). **Added:** docs sentence "the hosted console always opens on today (ET)". |
| C7 | Cloudflare header-echo probe never reaches the console once Access is in front (correctness, 3/3) | — | **Agree, FIX (Rock 5).** Cloudflare runbook probes locally first (`curl -si -H 'Host: <name>' http://127.0.0.1:8765/api/health`: 403 with `received_host` before `--trusted-host`, 200 after) and through Access with `cloudflared access curl -si https://<name>/api/health`. Plain `curl` from a tailnet device stays for the Tailscale runbook. |
| C8 | Renderer log paths follow the invoking shell's env while the agent gets a minimal env (correctness, 3/3) | #11 | **Already in rev 2** (resolved `ROOT` stamped into the plist env). **Added:** explicit precedence `--cron-tracking-root` > `$DCA_CRON_TRACKING_ROOT` > `dca_paths` default, and a test with the env var monkeypatched and no flag. |
| C9 | Phase A cleartext + same credential into Phase B (correctness, 3/3) | #3 | Duplicate of C1; same resolution. |
| C10 | `kill %1` / `wait %1` inside `( … )` is a no-op in zsh, so every proof's stop step was a false pass (operability, 3/3) | — | **Agree, FIX (all proofs).** Proofs capture `pid=$!`, poll `/api/health` until up, `kill -TERM "$pid"; wait "$pid"`, and Rock 3 asserts the port is released (`! lsof -nP -iTCP:8765 -sTCP:LISTEN`). Proofs are written for zsh (macOS default) and run there. |
| C11 | Runbooks apply a changed plist with `kickstart -k` (operability, 3/3) | #10 | **Already in rev 2** (bootout then bootstrap). **Added:** `--install` prints the apply pair (`bootout … 2>/dev/null \|\| true`, then `bootstrap`) with a test that bootout precedes bootstrap in the printed sequence, and the DOCS-OK proof greps for `launchctl bootout`. |
| R1 | `sys.path.insert` precedent trips E402 in linted `deploy/` (security, 0/3) | — | Refuted (the precedent already carries `# noqa: E402` and is linted by CI). The renderer will use the same `# noqa: E402` convention. |
| R2 | `HANDOFF.md` contradicts the remote-access rule (invariants, 0/3) | — | Refuted (it records the past loopback default, which remains the default). Non-goal stands. |
| R3 | Rock 4 proof passes even if the agent cannot read the TCC-protected `~/Documents` checkout (operability, 1/3) | — | Refuted as a false-pass (the agent would exit before any socket exists), but the underlying hazard is real for a checkout under `~/Documents`. **Adopted as a runbook troubleshooting note:** if `launchd.err.log` shows `Operation not permitted`, grant the interpreter Files and Folders / Full Disk Access in System Settings, or keep the checkout outside `~/Documents`. |
| R4 | `--behind-proxy` is mandatory in both runbooks although neither front needs it (operability, 1/3) | — | Refuted as harmful (the flag is harmless), but the wording improves: the runbooks now add `--behind-proxy` only if the probe's 403 body shows the name under `forwarded_host` rather than `received_host`. |

## Round 2 — revised plan submitted

Timestamp: 2026-09-13T05:17:24Z
PLAN.md sha256: f70c8137468d7a81…  VTO.md sha256: 80b5d096df300f6a…  DEFERRED.md sha256: 4047692bd9fa01a8…
Invocation: `codex exec resume <session> -m gpt-5.6-sol --sandbox read-only --json`

### Prompt sent (Round 2)

> Round 2. PLAN.md, VTO.md and DEFERRED.md have been revised (PLAN.md is now "Revision 3")
> to address every finding from your round-1 review. Re-read all three files end to end;
> the "Changes since round 1" section at the end of PLAN.md is the index. You remain
> read-only — propose plan changes, not file edits.
>
> Per-finding resolutions:
>
> 1. credential-in-curl-argv — every proof now passes credentials with
>    `curl -K <(printf 'user = "%s"\n' "$(<"$AUTH")")`; argv holds only a /dev/fd path.
> 2. fail-closed-only-at-cli — the whole matrix is enforced in `ConsoleAccess.__post_init__`;
>    `main()` maps ValueError to parser.error; new test asserts `make_server` with an invalid
>    access raises before `ThreadingHTTPServer` is constructed.
> 3. plaintext-lan-basic-auth — bounded, not eliminated (the operator explicitly chose
>    LAN-first for build/test): Phase A is foreground-only (the renderer has no `--bind`),
>    uses a disposable credential rotated before Phase B, the runbook and the startup banner
>    both state it is plaintext; in-process TLS stays deferred with that reasoning in
>    DEFERRED.md.
> 4. cross-site-get-exception-writes — the navigation tolerance applies only to `GET /`;
>    every `/api/*` GET stays cross-site-rejected; test asserts no summary is written and
>    `app.snapshot` is never invoked.
> 5. origin-not-bound-to-host — `Origin`, when present, must equal `http(s)://` + the
>    effective authority (accepted Host, or accepted X-Forwarded-Host under --behind-proxy);
>    mismatched-alias tests added.
> 6. auth-file-confidentiality-unproved — POSIX-only (non-POSIX raises), `lstat` + `S_ISREG`,
>    owner check; tests for symlink, FIFO, directory, wrong owner, non-POSIX.
> 7. hosted-default-date-freezes — `ConsoleApp.default_day()` rolls in local mode; used by
>    bootstrap, snapshot default and default_date; midnight rollover test (Rock 3).
> 8. restart-breaks-job-buttons — app.js re-bootstraps once on a 403 from POST /api/jobs
>    and retries once (a 403 is returned before submit(), so the job was never accepted);
>    manual kickstart proof (Rock 3).
> 9. launchd-drops-octopus-input — renderer `--octopus-run DIR` passthrough; VTO narrowed.
> 10. kickstart-does-not-reload-plist — bootout then bootstrap whenever the plist changed;
>     the renderer prints that apply pair; kickstart -k only for unchanged definitions.
> 11. custom-root-splits-service-output — one resolved ROOT (precedence flag > env > default)
>     for both the plist environment and the log paths; tests for flag and env cases.
> 12. custom-label-is-an-unsafe-target — KILLED: `--label` removed, fixed label.
> 13. verification-omits-coverage-gate — CI's exact `--cov-fail-under=90` command added.
> 14. focused-tests-variable-is-never-set — explicit `CONSOLE_TESTS="…"` in every proof.
> 15. new-files-escape-local-literal-scan — `git add -N` before the invariants test.
> 16. cloudflared-ingress-is-incomplete — terminal `http_status:404` catch-all and
>     `cloudflared tunnel ingress validate`.
> 17. renderer-cannot-produce-phase-a — renderer has no `--bind`; only the Phase B form is claimed.
> 18. hosted-success-is-not-gated — VTO redefined: code-complete (in-cycle, gated by commit
>     approval) vs deployed (operator completes the acceptance checklist after installing and
>     logging in to a front, which the Visionary cannot do); checklist added to PLAN.md.
>
> Additional changes from the Visionary's independent adversarial review (see the end of
> PLAN.md): bounded/redacted/rate-limited auth-failure log line; plaintext warning in a pure
> `startup_banner()`; renderer `--python PATH` so tests never assume `.venv` exists (CI has
> none); all proofs stop the console by pid because `kill %1` inside a subshell is a no-op in
> zsh, and Rock 3 checks the port is released; the Cloudflare probe runs locally first and
> then through `cloudflared access curl` (a plain curl is answered at the edge);
> `--behind-proxy` is added only when the probe shows the name under `forwarded_host`; a TCC
> troubleshooting note for checkouts under `~/Documents`.
>
> For each finding from the previous round, confirm whether it is resolved or still open,
> using the same [SEVERITY] short-title label you used before. New findings are allowed but
> should be rare this late — flag anything major. End with the same single verdict line as
> before, as the literal last line of your reply:
> `VERDICT: SAME PAGE` if you have zero open FIX or KILL items, otherwise
> `VERDICT: BLOCKED — N open finding(s)`.

### PLAN.md revision 3 as reviewed

````markdown
# PLAN — Host the existing DCA console as a private web app

Build plan for the `rocket-fuel/hosted-console` cycle. Vision and non-negotiables live in
`VTO.md`; deferred items in `DEFERRED.md`. This plan is the subject of the Same Page
Meeting and is revised on the record until `VERDICT: SAME PAGE`. **Revision 3** (after
Same Page round 1 and the Visionary's independent four-lens adversarial panel; the
per-finding changes are listed at the end).

## Starting point (facts, verified 2026-09-13)

- `dca_console.py` (502 lines): `make_server()` at line 317 builds
  `ThreadingHTTPServer(("127.0.0.1", port), Handler)` (line 412). `_trusted()` (339-345)
  accepts only `Host` in `{127.0.0.1:<port>, localhost:<port>}`, `Origin` matching
  `http://<those>`, and rejects `Sec-Fetch-Site: cross-site`. `main()` (415-498) has
  `--demo/--local/--root/--output-dir/--octopus-run/--research-python/--date/--port/
  --no-browser/--once/--run/--check`; it freezes `Settings.day` at startup (442) and
  `/api/bootstrap` (357) hands that frozen day to the browser as `default_date`. POST
  `/api/jobs` requires `X-Console-Token` (`secrets.compare_digest`, 390) **before**
  `submit()` runs, so a 403 proves no job was accepted. Only `KeyboardInterrupt` is handled
  around `serve_forever` (492-497). `/api/snapshot` and `/api/export` are GETs with a side
  effect: `snapshot()` calls `_save_summary()` (193-197).
- `dca_console_jobs.py`: `redact()` (21-32) masks env values whose names contain
  `SECRET|TOKEN|PASSWORD|API_KEY|ACCOUNT_NUMBER`, labelled secrets, and any 6-12 digit run.
  `_command()` in `dca_console.py` (236-247) copies `dict(os.environ)` into every job child.
- `console/app.js` bootstraps the token only when `state.token` is empty; after a server
  restart an open tab keeps posting a dead token.
- Job history is bounded (30 jobs, 24 000 log chars); the snapshot cache clears at 32
  entries; `ConsoleApp.signatures` grows one key per day, unbounded.
- Tests: `tests/test_dca_console.py` (fixtures `app`, `http_server` via
  `make_server(app, 0)`, helpers `request()`/`post()`), plus `test_dca_console_data.py`,
  `test_dca_console_jobs.py`, `test_dca_console_octopus.py`,
  `test_console_security_logging.py` — 78 passing.
- Console modules are deliberately absent from `pyproject.toml` `py-modules` (lines
  140-142): checkout-only. `deploy/` is not shipped, but `deploy/*.py` **is** linted by
  ruff and counted by coverage (`deploy` is in neither exclude list). CI's coverage gate is
  `pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90`.
- `tests/test_config_invariants.py` scans `git ls-files`, so new files are invisible to a
  local run of it until they are at least intent-to-add (`git add -N`).
- The live artifact tree is `<checkout>/cron_tracking` on this Mac
  (`dca_paths.cron_tracking_root()` with `DCA_CRON_TRACKING_ROOT` unset). No launchd
  precedent in the repo; `deploy/run-drift-check.sh` explains why service logic lives in
  a script rather than inline in a unit.
- `docs/local_console.md` lines 149-153 say the server "binds only to loopback" and
  "Do not expose it through a public reverse proxy." Line 94: "The console does not
  schedule or submit live brokerage orders."

## Phases and what "done" means

- **Phase A (build/test, foreground only):** `python3 from.py --local --no-browser
  --bind <LAN-IP> --auth-file <disposable file>` on the home wifi. **Plaintext HTTP**: the
  credential and console data cross the LAN unencrypted. Bounded by: a disposable
  credential rotated before Phase B; never run as the launchd service (the renderer cannot
  emit `--bind`); the runbook says so in its first sentence. In-process TLS is the proper
  fix and is deferred.
- **Phase B (deploy):** default loopback bind + `--auth-file … --trusted-host <name>
  --behind-proxy`, fronted by Tailscale Serve **or** Cloudflare Tunnel + Access. Same code
  path; only the runbook differs.
- Run as a launchd login agent on this Mac in `--local --no-browser` mode.
- **Code-complete** (this cycle, gated by commit approval): all Rocks accepted, LAN
  acceptance demonstrated from a second device, launchd cycle demonstrated, runbooks
  written. **Deployed** (operator step, post-merge): the operator installs and logs in to
  Tailscale or Cloudflare and completes the acceptance checklist at the end of this plan.
  The Visionary cannot perform account creation or login on the operator's behalf.

## Design decisions

| Topic | Decision | Why |
|---|---|---|
| Secret source | `--auth-file PATH`: one line `user:password`; POSIX only (`os.name == "posix"`, otherwise `ValueError` — fail closed, no Windows tolerance for this feature); `os.lstat` must be a regular file (`S_ISREG`; symlinks, FIFOs, directories rejected); `st_uid == os.getuid()`; `st_mode & 0o077 == 0`; username printable ASCII without `:`; password ≥ 12 chars. Errors name the rule, never the contents | An env var would be copied into every job child by `env = dict(os.environ)` (`dca_console.py:237`), show in `launchctl print` / `ps -E`, and sit inside the plist. A path in argv leaks nothing. Ownership and regular-file checks are what make "0600" mean something |
| Fail-closed construction | All access rules are enforced in `ConsoleAccess.__post_init__`, so an invalid combination cannot be constructed anywhere — not in `main()`, not by a future caller of `make_server()`. `main()` maps `ValueError` → `parser.error` (exit 2) | A CLI-only check would let another entry point open a reachable socket without a credential |
| Auth scheme | HTTP Basic on every route except `/api/health`, only when a credential is configured; `secrets.compare_digest` on `b"user:password"`; failure → `401` + `WWW-Authenticate: Basic realm="DCA console", charset="UTF-8"` + JSON error | Native browser prompt on a phone; zero frontend change for auth because same-origin `fetch` (`console/app.js:381,384,411`) and the `<a download>` export (`console/index.html:38`) attach credentials automatically |
| `/api/health` unauthenticated | Still behind the Host/Origin check; body is only `{"status","mode"}` | Lets probes and the launchd runbook work without embedding the password anywhere |
| `X-Console-Token` | Keep, plus a frontend re-bootstrap: on a 403 from `POST /api/jobs` the page clears `state.token`, fetches `/api/bootstrap` once and retries once | Basic credentials are ambient like cookies; the token (fetched same-origin, no CORS headers) is the CSRF defence. A launchd restart rotates it; `do_POST` checks the token before `submit()` (`dca_console.py:390`), so a 403 proves the job was never accepted and one retry is safe |
| Rolling default day | `ConsoleApp.default_day()`: `datetime.now(ET).date()` in local mode, `settings.day` in demo mode; used by `/api/bootstrap`, the `snapshot()` default and `default_date` | `main()` freezes `Settings.day` at startup (442); a service that survives midnight would otherwise open new tabs on stale data |
| `--bind ADDR` | IPv4 literal only (`ipaddress.IPv4Address`), default `127.0.0.1`; foreground use only — the launchd renderer has no `--bind`. A non-loopback bind makes the startup banner (pure `startup_banner(access, port)`) print `plain HTTP: the Basic credential and console token are visible to anyone on this network` | `HTTPServer` is AF_INET; hostnames/IPv6 deferred; the persistent service stays loopback-only; the operator must see the plaintext caveat where they start the process |
| `--trusted-host HOST[:PORT]` (repeatable) | lowercased; hostname regex; no scheme, path, wildcard or whitespace; port ≤ 65535 | Extra names accepted in `Host`. Also accept `https://` origins (today only `http://`, `dca_console.py:344`, which would block POSTs behind an HTTPS proxy) |
| `hosts(port)` | `{127.0.0.1:port, localhost:port}` ∪ `{bind:port}` when bind is a specific non-loopback IP ∪ `trusted_hosts`; `0.0.0.0` adds nothing | Phase A needs no `--trusted-host`; with `0.0.0.0` the server cannot know which address the client typed |
| Effective authority and `Origin` | The *effective authority* is the accepted `Host`, or under `--behind-proxy` the accepted `X-Forwarded-Host`. When `Origin` is present it must equal exactly `http://<authority>` or `https://<authority>` | Membership of both in allowed sets is not enough: a loopback `Host` with a tailnet `Origin` (or vice versa) must be refused |
| `--behind-proxy` | Additionally accept a **configured** name (never a loopback name) from `X-Forwarded-Host`; requires ≥1 `--trusted-host` **and** a loopback bind | A LAN-reachable socket that honours forwarded headers would let any LAN client forge them |
| Fail-closed matrix (in `__post_init__`) | non-loopback bind without a credential; any `trusted_hosts` without a credential; `behind_proxy` without `trusted_hosts` or with a non-loopback bind; `0.0.0.0` without `trusted_hosts`; malformed bind / host values | Nothing listens on a reachable address without a credential |
| `Sec-Fetch-Site: cross-site` | Rejected everywhere **except** `GET /` with `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. Every `/api/*` route, including GETs, stays cross-site-rejected | Cloudflare Access login is a redirect chain through `<team>.cloudflareaccess.com`; Fetch Metadata computes the site over the whole chain, so the first document load after login arrives `cross-site`. The document route is side-effect-free; `/api/snapshot` and `/api/export` are not (they call `_save_summary()`), so they keep the strict rule. Existing test at `tests/test_dca_console.py:122-124` still gets 403 |
| 403 body | adds `received_host` (≤ 253 chars) and `forwarded_host` | The runbook's "what does the backend see" probe; JSON with `nosniff`, no HTML |
| Brute-force backoff | Deferred (see `DEFERRED.md`); 12-char minimum, documented generator | Behind either proxy every request is from `127.0.0.1`; per-IP state becomes a global lockout lever |
| Auth-failure log line | One fixed-shape stderr line per failure: `redact()` of client address, method and `urlsplit(path).path[:128]` (route only, never the query); rate-limited to one line per 5 s per process, with a suppressed count on the next line emitted | Any unauthenticated LAN/tailnet client can hit 401/403 at line rate; the line must not let it grow `launchd.err.log` unboundedly or plant a credential-shaped string in it |
| SIGTERM | Context manager installs a handler that raises `KeyboardInterrupt`, reusing the Ctrl+C teardown (`dca_console.py:492-497`); restores the previous handler; wraps only the serving branch | `server.shutdown()` from the handler would deadlock (same thread as `serve_forever`). Worst-case teardown ≈ 11 s (`dca_console_jobs.py:210-226`), inside the plist's `ExitTimeOut = 30` |
| Handler hygiene | `Handler.timeout = HANDLER_TIMEOUT_SECONDS` (30; today `None`, only POST bodies get `settimeout(5)` at line 400); `version_string()` override | An idle LAN/proxy connection must not pin a thread; `Server:` should not advertise the Python version |
| Long uptime | Cap `ConsoleApp.signatures` to the last 32 days | Everything else is already bounded |
| launchd | `deploy/launchd/render_console_agent.py` renders the plist with `plistlib` (`--print` / `--install`), validates the auth file via `load_auth_file`, takes `--python PATH` (default `<checkout>/.venv/bin/python`; refuses when the chosen interpreter does not exist), resolves **one** artifact root with precedence `--cron-tracking-root` > `$DCA_CRON_TRACKING_ROOT` > `dca_paths` default, always writes it into the plist environment and derives the log paths from that same value, creates the log directory, prints the `launchctl` commands including the apply pair (`bootout … 2>/dev/null || true`, then `bootstrap`). Fixed label `com.all-stock-dca.console`. Optional `--octopus-run DIR` passthrough. No `--bind`. No placeholder template | Same "logic in a script, not the unit" rationale as `deploy/run-drift-check.sh:6-17`; `plistlib` cannot mis-escape; one resolved root means logs and console artifacts can never land in different trees; a fixed label removes an unvalidated path/shell fragment |
| pyproject | No change | Console is checkout-only; `deploy/` is not shipped |
| Proxy `Host` semantics (read from source at HEAD, not a live install — uncertainty acknowledged) | Tailscale Serve preserves `Host` and adds `X-Forwarded-Host/-Proto/-For` + `Tailscale-User-*`; cloudflared preserves `Host` unless `httpHostHeader` is set | Accept the configured name in `Host` or (with `--behind-proxy`) `X-Forwarded-Host`; every runbook starts with a `curl -si …/api/health` whose 403 body prints what arrived |
| Credentials in proofs | `curl -K <(printf 'user = "%s"\n' "$(<"$AUTH")")` | The curl config arrives through a `/dev/fd` path; argv never holds the secret |

## Rocks

Sequencing: 1 → 2 → 3; 4 after 1 (parallel with 3); 5 last.

Every proof block starts with these two assignments (fresh shells have neither):

```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"
AUTH=~/.config/all-stock-dca/console-auth
```

Credential file created once (Phase A, disposable — rotated before Phase B):

```bash
mkdir -p ~/.config/all-stock-dca && umask 077 && printf 'dca:%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" > ~/.config/all-stock-dca/console-auth
```

Fixture passwords and hosts are letters-and-hyphens (`s3cret-pw-abcdefg`, `192.0.2.10`,
`mac.tailnet.ts.net`) so the account-shaped literal scan stays quiet.

### Rock 1 — Basic auth from a 0600 file (loopback-safe, no exposure yet)

**Scope.** Frozen `ConsoleAccess` dataclass beside `Settings` (`username: str = ""`,
`credential: bytes | None = field(default=None, repr=False)`) with `__post_init__`
validation of the credential shape; `load_auth_file(path) -> tuple[str, bytes]` with the
rules above (POSIX only, `lstat` + `S_ISREG`, owner, mode, one line, `:`), error messages
naming the rule but never the contents; `ConsoleAccess.authorized(headers)` (`Basic <b64>`,
scheme case-insensitive, `base64.b64decode(..., validate=True)`, `compare_digest`);
`--auth-file`; 401 path on every route except `/api/health` — order `trusted` → 403,
`authorized` → 401, then token → 403 on POST; `dca_console_jobs.register_secret()`
consulted by `redact()`; the bounded, redacted, rate-limited auth-failure line from the
design table. `make_server(app, port, access=None)`
keeps today's behaviour by default so all 78 existing tests pass untouched. Frontend untouched.

**Non-goals.** `--bind`, trusted hosts, proxy headers (Rock 2); backoff; UI hint that
auth is on.

**Files.** `dca_console.py`, `dca_console_jobs.py`, `tests/test_dca_console.py`.

**Tests.** `test_auth_file_rules_fail_closed` (missing, mode 0644, symlink to a good file,
FIFO, directory, wrong owner via monkeypatched `os.getuid`, non-POSIX via monkeypatched
`os.name`, no colon, empty, two lines, 11-char password → `ValueError` whose message lacks
the file contents; good file → `("dca", b"dca:s3cret-pw-abcdefg")`);
`test_basic_auth_required_on_every_route_except_health` (every asset, `/api/bootstrap`,
`/api/snapshot`, `/api/export`, `/api/docs` → 401 with `WWW-Authenticate` starting
`Basic realm=`; `/api/health` → 200; POST with right token but no credentials → 401 and
`app.submit` never called); `test_basic_auth_wrong_credentials_are_rejected` (wrong
password, wrong user, undecodable base64, `Bearer` scheme → 401);
`test_basic_auth_correct_credentials_pass_and_token_still_required` (bootstrap 200; POST
with credentials but no token → 403; with both → 202);
`test_password_never_appears_in_responses_or_redaction` (bodies/headers lack it; `redact()`
masks it; `repr(access)` lacks it); `test_auth_failure_is_logged_without_credentials`;
`test_auth_failure_log_line_is_bounded_and_redacted` (a 60 KB request path and a path
containing the fixture password each yield one short line with the route only and the
password masked; a burst of 50 failures yields at most two lines plus a suppressed count).

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"; AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_config_invariants.py -q \

.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done
curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/health
curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/bootstrap
curl -s -o /dev/null -w '%{http_code}\n' -K <(printf 'user = "%s"\n' "$(<"$AUTH")") http://127.0.0.1:8765/api/bootstrap
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
```
Expected: ruff clean; 85 console passes (78 + 7) and the invariants file green; curl
prints `200 401 200`; then `Stopping local console.` and `exit=0`.

### Rock 2 — `--bind`, `--trusted-host`, `--behind-proxy`, hardened handler (Phase A reachable)

**Scope.** Extend `ConsoleAccess` with `bind`, `trusted_hosts`, `behind_proxy` and move
the whole fail-closed matrix into `__post_init__`; pure `hosts(port)`,
`effective_authority(headers, port) -> str | None` and `trusted(headers, port)`; replace
`_trusted()` (`dca_console.py:339-345`); `Origin` bound to the effective authority with
`http://` or `https://`; cross-site tolerance for `GET /` navigations only; 403 diagnostic
body; `ThreadingHTTPServer((access.bind, port), …)` (line 412); `HANDLER_TIMEOUT_SECONDS
= 30`; `version_string()`; pure `startup_banner(access, port)` replaces the banner at 472-473 and prints bind,
port, trusted hosts, `auth: basic user=<name>` (never the password) and, for a
non-loopback bind, the plaintext warning line; reword the `--port` help (426) and the
bind-failure message (470); `webbrowser.open` targets the bind IP when it is a specific
address.

**Non-goals.** IPv6 / hostname binds; in-process TLS; proxy identity headers; UI changes.

**Files.** `dca_console.py`, `tests/test_dca_console.py`.

**Tests.** `test_console_access_construction_fails_closed` (`ConsoleAccess(...)` raises
`ValueError` for the whole matrix plus `bind="not-an-ip"`, `"::1"`, trusted-host values
`http://x`, `x/y`, `x:99999`, `x y`, `*.ts.net`);
`test_invalid_access_never_constructs_a_server` (`ThreadingHTTPServer` monkeypatched to
`pytest.fail`; `make_server(app, 0, ConsoleAccess(bind="192.0.2.10"))` raises first);
`test_exposure_flags_fail_closed_from_cli` (`main([...])` → `SystemExit` code 2 for
`--bind 192.0.2.10` without `--auth-file`, `--trusted-host x` without `--auth-file`,
`--behind-proxy` without `--trusted-host`, `--behind-proxy --bind 192.0.2.10 …`,
`--bind 0.0.0.0 --auth-file …` without `--trusted-host`);
`test_trusted_hosts_accept_configured_names_and_https_origin` (`Host: mac.tailnet.ts.net`
+ `Origin: https://mac.tailnet.ts.net` + credentials → 200; upper-case Host → 200;
`Host: other.ts.net` → 403 with `received_host == "other.ts.net"`; loopback Host still 200);
`test_origin_must_match_effective_authority` (loopback `Host` + `Origin:
https://mac.tailnet.ts.net` → 403; tailnet `Host` + `Origin: http://127.0.0.1:<port>` → 403;
under `behind_proxy`, `X-Forwarded-Host: mac.tailnet.ts.net` + `Origin:
https://mac.tailnet.ts.net` → 200);
`test_forwarded_host_is_ignored_unless_behind_proxy` (`Host: untrusted.example` +
`X-Forwarded-Host: mac.tailnet.ts.net` → 403 on a non-proxy server, 200 with
`behind_proxy=True`; `X-Forwarded-Host: 127.0.0.1:<port>` on the proxy server → 403);
`test_hosts_for_bind_addresses` (pure);
`test_non_loopback_bind_banner_warns_about_plaintext` (pure `startup_banner`: the line
appears for `bind="192.0.2.10"`, not for loopback, and never contains the password);
`test_cross_site_navigation_allowed_only_for_document_route` (GET `/` with
`Sec-Fetch-Site: cross-site, Sec-Fetch-Mode: navigate, Sec-Fetch-Dest: document` → 200;
the same headers on `/api/bootstrap`, `/api/snapshot` and `/api/export` → 403, no summary
file is written and a monkeypatched `app.snapshot` is never invoked; `Sec-Fetch-Mode:
cors` on `/` → 403; POST → 403);
`test_idle_connection_is_closed_by_handler_timeout` (`HANDLER_TIMEOUT_SECONDS = 0.2`, raw
socket that sends nothing gets EOF within 2 s); `test_server_header_does_not_leak_python_version`.

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"; AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_config_invariants.py -q \

LAN=$(ipconfig getifaddr en0)
.venv/bin/python from.py --local --no-browser --bind "$LAN" --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null "http://$LAN:8765/api/health" && break; sleep 0.25; done
curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/health"
curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/snapshot"
curl -s -o /dev/null -w '%{http_code} ' -K <(printf 'user = "%s"\n' "$(<"$AUTH")") "http://$LAN:8765/api/snapshot"
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' "http://$LAN:8765/api/health"
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
.venv/bin/python from.py --local --no-browser --bind 0.0.0.0; echo "exit=$?"
```
Expected: ruff clean; 96 console passes (85 + 11); the banner shows the plaintext
warning; curl prints `200 401 200 403`; `Stopping local console.` then `exit=0`; the last
command prints the fail-closed message and `exit=2`. Then a phone on the same wifi:
`http://<LAN>:8765` prompts once, shows `LOCAL` mode with real runs, and "Export JSON"
downloads. (Plaintext on the home wifi; disposable credential.)

### Rock 3 — Clean SIGTERM, restart resilience, long-uptime bounds

**Scope.** `_sigterm_as_keyboard_interrupt()` context manager around `serve_forever`
(line 491), restoring the previous handler, wrapping only the serving branch;
`ConsoleApp.default_day()` (rolling in local mode) used by `/api/bootstrap`, `snapshot()`
and `default_date`; cap `signatures` (129, 225) to the last 32 days; `console/app.js`:
on a 403 from `POST /api/jobs`, clear `state.token`, re-bootstrap once, retry once, then
surface the error.

**Non-goals.** SIGHUP reload; job-file pruning; log rotation.

**Files.** `dca_console.py`, `console/app.js`, `tests/test_dca_console.py`.

**Tests.** `test_sigterm_stops_console_cleanly` (posix-only subprocess: spawn
`from.py --local --no-browser --port 0 --root <tmp> --output-dir <tmp>`, wait for the
`DCA console (local)` banner, `send_signal(SIGTERM)`, `wait(timeout=20)` → returncode 0
and stdout contains `Stopping local console.`);
`test_sigterm_handler_raises_and_restores_previous_handler`;
`test_local_mode_default_day_rolls_over_midnight` (monkeypatched clock: bootstrap
`default_date` and `snapshot()["daily"]["date"]` change from one ET day to the next
without a restart; demo mode stays on `settings.day`);
`test_summary_signature_cache_is_bounded` (100 distinct days → `len(app.signatures) <= 32`).

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"; AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS -q \

.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
! lsof -nP -iTCP:8765 -sTCP:LISTEN && echo "port released"
```
Expected: 100 passes; `Stopping local console.` then `exit=0` within seconds, then
`port released`. Manual (after Rock 4): with a tab open, `launchctl kickstart -k
gui/$(id -u)/com.all-stock-dca.console`, then click "Test DCA pipeline" in the same tab
without reloading → the job starts.

### Rock 4 — launchd agent renderer

**Scope.** `deploy/launchd/render_console_agent.py` (stdlib `argparse`, `plistlib`, `os`,
`pathlib`; `sys.path.insert(1, <checkout>)` per the ticker-advisor precedent, then
`from dca_paths import cron_tracking_root` and `from dca_console import load_auth_file`).
Args: `--auth-file` (required), `--python PATH` (default `<checkout>/.venv/bin/python`;
the chosen interpreter must exist), `--port 8765`, `--trusted-host` (repeatable),
`--behind-proxy`, `--octopus-run DIR` (optional, must be an existing directory),
`--cron-tracking-root DIR` (optional), exactly one of `--print` / `--install`. No `--bind`,
no `--label`. The renderer resolves `ROOT` once with precedence `--cron-tracking-root` >
`$DCA_CRON_TRACKING_ROOT` > `dca_paths.cron_tracking_root()` default, and uses it for both
the plist environment and the log paths, so the plist is self-consistent whatever the
invoking shell exported. Rendered dict:

```
Label                = com.all-stock-dca.console
ProgramArguments     = [<python>, <checkout>/from.py, --local, --no-browser,
                        --port, 8765, --auth-file, <abs path>, (--trusted-host, H)*, (--behind-proxy),
                        (--octopus-run, <abs dir>)]
WorkingDirectory     = <checkout>
RunAtLoad            = true
KeepAlive            = { SuccessfulExit = false }   # restart on failure; a clean SIGTERM exit (main returns 0) is not restarted
ThrottleInterval     = 15                            # a persistent bind failure (main returns 1) retries every 15 s, no hot loop
ExitTimeOut          = 30
ProcessType          = Background
StandardOutPath      = <ROOT>/console/launchd.out.log
StandardErrorPath    = <ROOT>/console/launchd.err.log
EnvironmentVariables = { PYTHONUNBUFFERED = "1", PATH = "/usr/bin:/bin:/usr/sbin:/sbin", DCA_CRON_TRACKING_ROOT = <ROOT> }
```

No secret anywhere in the plist. `--install` writes
`~/Library/LaunchAgents/com.all-stock-dca.console.plist` (0644), creates
`<ROOT>/console/` (launchd creates log files but not their parent), and prints:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist   # load + start
launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state|pid'             # status
launchctl kickstart -k gui/$(id -u)/com.all-stock-dca.console                            # restart an UNCHANGED definition (e.g. after rotating the auth file)
launchctl bootout gui/$(id -u)/com.all-stock-dca.console                                 # stop + unload; REQUIRED before bootstrap whenever the plist changed
# apply pair, printed last, for a changed plist:
launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist
```

`.gitignore` gains `deploy/launchd/*.plist` beside the existing `deploy/*.env` line.

**Non-goals.** Running `launchctl` from the script; a `/Library/LaunchDaemons` variant;
a LAN-bound (Phase A) service; installing Tailscale or cloudflared.

**Files.** `deploy/launchd/render_console_agent.py` (new), `tests/test_console_launchd.py`
(new; imports the script via `importlib.util.spec_from_file_location` so coverage counts
it), `.gitignore`.

**Tests.** Every test passes `--python sys.executable` (CI has no `.venv`; no test may
assume `<checkout>/.venv` exists). `test_renders_expected_program_arguments_and_paths`
(loopback-only Phase B form with and without `--trusted-host mac.tailnet.ts.net
--behind-proxy` and `--octopus-run`; `plistlib.loads` output has the exact
`ProgramArguments` with `[0] == sys.executable`,
`Label == "com.all-stock-dca.console"`, `KeepAlive == {"SuccessfulExit": False}`,
`RunAtLoad`, `ExitTimeOut == 30`, log paths under `<ROOT>/console`, no
`EnvironmentVariables` key containing `PASSWORD`);
`test_custom_root_is_used_for_both_environment_and_logs` (`--cron-tracking-root <tmp>` →
`EnvironmentVariables["DCA_CRON_TRACKING_ROOT"] == <tmp>` and both log paths start with
`<tmp>/console/`); `test_root_from_environment_is_used_when_flag_absent` (monkeypatched
`DCA_CRON_TRACKING_ROOT`, no flag → the same root in the env entry and both log paths);
`test_rendered_plist_contains_no_secret`; `test_renderer_fails_closed` (loose-mode auth
file, `--behind-proxy` without `--trusted-host`, `--octopus-run` that is not a directory,
`--python` pointing at a missing file, and the default interpreter missing when no
`--python` is given → exit 2; no `--bind` / `--label` arguments exist);
`test_install_writes_agent_and_creates_log_dir` (`HOME` → tmp; 0644; log dir exists);
`test_install_prints_bootout_before_bootstrap` (the printed apply sequence has
`launchctl bootout` before `launchctl bootstrap`).

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"; AUTH=~/.config/all-stock-dca/console-auth
git add -N deploy/launchd tests/test_console_launchd.py \
&& .venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_console_launchd.py tests/test_config_invariants.py -q \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --print | plutil -lint - \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --install \
&& { launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true; } \
&& launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist \
&& for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done \
&& curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/health \
&& launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state = |pid = '
```
Expected: tests green (100 console + 7 launchd); `stdin: OK`; `200`; `state = running`
with a pid. Afterwards `launchctl bootout gui/$(id -u)/com.all-stock-dca.console` and
`tail -2 cron_tracking/console/launchd.out.log` shows `Stopping local console.`

### Rock 5 — Docs, runbooks, DEFERRED.md

**Scope.** `docs/local_console.md`: replace lines 149-153 with a **Remote access** section
that opens with "The console does not schedule or submit live brokerage orders" and states
the precise rule (loopback needs nothing; any non-loopback bind or external name requires
`--auth-file`; forwarded headers only with `--behind-proxy` on a loopback bind; never
`tailscale funnel`, never a bare port-forward; the launchd service is always loopback).
Sub-sections:
- **Phase A: home LAN (foreground, plaintext).** First sentence: this is plain HTTP on the
  home wifi and the credential crosses it on every 15-second refresh; use a disposable
  credential and rotate it before Phase B; never install it as the service. Note that
  binding the Tailscale interface IP instead of the LAN IP avoids cleartext entirely once
  Tailscale is installed. The hosted console always opens on today (ET).
- **Phase B: Tailscale Serve.** `tailscale serve --bg http://127.0.0.1:8765` →
  `tailscale serve status` (no Funnel) → header-echo probe from a tailnet device
  (`curl -si https://<mac>.<tailnet>.ts.net/api/health`; the 403 body shows the name under
  `received_host` or `forwarded_host`) → rotate the credential → re-render with
  `--trusted-host <mac>.<tailnet>.ts.net`, adding `--behind-proxy` only if the probe showed
  the name under `forwarded_host` → the apply pair `launchctl bootout` then `bootstrap` (a
  changed plist is never applied by `kickstart`).
- **Phase B: Cloudflare Tunnel + Access.** Local pre-check of the Host semantics before
  Access is in the loop: `curl -si -H 'Host: console.<domain>' http://127.0.0.1:8765/api/health`
  (403 with `received_host` before `--trusted-host`, 200 after) → `cloudflared tunnel
  create` → Access application + policy **before** `tunnel route dns` (the name is never
  public with Basic alone) → `config.yml` with the ingress to `http://127.0.0.1:8765` **and
  the mandatory terminal catch-all `- service: http_status:404`** → `cloudflared tunnel
  ingress validate` → `cloudflared service install` → header-echo probe **through Access**
  (`cloudflared access curl -si https://console.<domain>/api/health`; a plain `curl` is
  answered at Cloudflare's edge with a 302 to the Access login and never reaches the
  console) → rotate the credential → re-render with `--trusted-host console.<domain>`,
  adding `--behind-proxy` only if the probe showed the name under `forwarded_host` → the
  apply pair `bootout` then `bootstrap`. Note the expected double prompt (Access, then
  Basic) and why Basic stays: stdlib cannot verify the Access RS256 JWT.
- **Run as a login service (launchd).** Renderer usage, the four `launchctl` commands with
  the bootout-before-bootstrap rule, log locations, `--octopus-run` for forecasts, the
  sleep caveat (the Mac must not sleep), and a troubleshooting note: if `launchd.err.log`
  shows `Operation not permitted` reading the checkout, macOS TCC is blocking the
  interpreter's access to `~/Documents` — grant it Files and Folders / Full Disk Access in
  System Settings, or keep the checkout outside `~/Documents`.
- **Operator acceptance checklist** (copied from this plan's final section).
Add the new flags to the entry-points block (102-129), 401/403 semantics to the API table
(138-147), replace the stale "Verification for this change" (158-178) with the Rock
proofs and the coverage gate. Note that a network-mounted `--root` makes the 15-second
`rglob` the hot spot. `CLAUDE.md`: architecture row "Local console" (`from.py` →
`dca_console.py`, `dca_console_{data,jobs,octopus}.py`, `console/`, `deploy/launchd/`;
checkout-only, never places orders), doc-index row for `docs/local_console.md`, commands
block gains `python3 from.py --local --no-browser`. `docs/repo_map.md` §1.7 row + §4 entry
"see today's runs from my phone". `docs/README.md` row for `local_console.md` extended.
`MEMORY.md` "Loopback UI" → "loopback by default; LAN/proxy with `--auth-file`".
`DEFERRED.md` kept current.

**Non-goals.** `HANDOFF.md` (historical record); `scripts/agent_entry_points.py` (its
`from.py` entry is flag-agnostic).

**Files.** `docs/local_console.md`, `CLAUDE.md`, `docs/repo_map.md`, `docs/README.md`,
`MEMORY.md`, `DEFERRED.md`.

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"
git add -N deploy/launchd tests/test_console_launchd.py DEFERRED.md \
&& .venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_console_launchd.py tests/test_config_invariants.py scripts/tests/test_agent_entry_points.py -q \
&& ! grep -q 'Do not expose it through a public reverse proxy' docs/local_console.md \
&& grep -q 'does not schedule or submit live brokerage orders' docs/local_console.md \
&& grep -q 'http_status:404' docs/local_console.md && grep -q 'ingress validate' docs/local_console.md \
&& grep -q 'cloudflared access curl' docs/local_console.md && grep -q 'launchctl bootout' docs/local_console.md \
&& grep -q 'local_console.md' CLAUDE.md && grep -q 'from.py' docs/repo_map.md && test -f DEFERRED.md && echo DOCS-OK
```

## Whole-cycle verification (before commit approval)

```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"
git add -N deploy/launchd tests/test_console_launchd.py DEFERRED.md
ruff check .
.venv/bin/python -m pytest $CONSOLE_TESTS tests/test_console_launchd.py tests/test_config_invariants.py -q
.venv/bin/python -m pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90   # CI's exact gate, full suite (~5 min)
.venv/bin/python -m pytest scripts/tests -q
python run_dca.py --demo --report --now 2026-09-11T15:45 --artifacts-dir /tmp/dca-demo
```

Rules: proofs are written for zsh (the macOS default shell) and are run there; no test
may assume `<checkout>/.venv` exists (CI runs on the system interpreter); every proof that
starts the console stops it by pid and Rock 3 checks the port is released.

Manual, in-cycle: Rock 2's LAN sequence from a phone (plaintext, disposable credential);
Rock 4's launchd bootstrap / health / bootout cycle; Rock 3's open-tab job button after
`kickstart -k`.

## Operator acceptance checklist (post-merge; the cycle is *deployed* only when this passes)

1. Install Tailscale (or cloudflared) on this Mac and the phone; log in. (Operator step.)
2. `curl -si https://<name>/api/health` from the phone's browser or a tailnet device: a
   403 body shows `received_host` / `forwarded_host`; confirm the name to trust.
3. Rotate the credential file (regenerate it; the Phase A value crossed the LAN in plaintext).
4. Re-render with `--trusted-host <name> --behind-proxy`; `launchctl bootout`, then
   `bootstrap`; `launchctl print` shows `state = running`.
5. Phone, off the home wifi: `https://<name>/` prompts once (twice with Access), shows
   `LOCAL` mode with the latest scheduled runs, "Export JSON" downloads, and a job button
   starts a job.
6. Confirm `tailscale serve status` shows no Funnel (or the Cloudflare Access policy is
   attached to the hostname) before leaving it running.

## Changes since round 1 (Same Page Meeting)

1. credential-in-curl-argv → `curl -K <(…)` in every proof.
2. fail-closed-only-at-cli → matrix enforced in `ConsoleAccess.__post_init__`; server-never-constructed test.
3. plaintext-lan-basic-auth → Phase A is foreground-only, disposable credential, rotation step, plaintext stated; TLS deferred.
4. cross-site-get-exception-writes → exception only for `GET /`; `/api/*` GETs stay strict; side effect documented.
5. origin-not-bound-to-host → `Origin` must equal the effective authority; mismatch tests.
6. auth-file-confidentiality-unproved → POSIX-only, `lstat`/`S_ISREG`, owner check; symlink/FIFO/dir/owner/non-POSIX tests.
7. hosted-default-date-freezes → `ConsoleApp.default_day()` rolling in local mode; rollover test (Rock 3).
8. restart-breaks-job-buttons → `app.js` re-bootstrap-once on 403 (Rock 3); manual kickstart proof.
9. launchd-drops-octopus-input → renderer `--octopus-run` passthrough; VTO narrowed.
10. kickstart-does-not-reload-plist → bootout + bootstrap whenever the plist changed.
11. custom-root-splits-service-output → one resolved `ROOT` for env and logs; custom-root test.
12. custom-label-is-an-unsafe-target → `--label` removed (KILL).
13. verification-omits-coverage-gate → CI's coverage command in whole-cycle verification.
14. focused-tests-variable-is-never-set → explicit `CONSOLE_TESTS=` in every proof.
15. new-files-escape-local-literal-scan → `git add -N` before the invariants test.
16. cloudflared-ingress-is-incomplete → catch-all `http_status:404` + `ingress validate`.
17. renderer-cannot-produce-phase-a → renderer has no `--bind`; only the Phase B form is claimed.
18. hosted-success-is-not-gated → code-complete vs deployed; operator acceptance checklist.

Additional changes from the Visionary's independent adversarial panel (recorded in
`SAME-PAGE-LOG.md`):

- Auth-failure stderr line is fixed-shape, route-only, `redact()`ed and rate-limited (Rock 1).
- Startup banner warns about plaintext when the bind is non-loopback; pure `startup_banner()` (Rock 2).
- Cross-site navigation test also asserts `app.snapshot` is never invoked for `/api/*` (Rock 2).
- Renderer gains `--python PATH`; tests never assume `.venv` exists; root precedence
  `--cron-tracking-root` > `$DCA_CRON_TRACKING_ROOT` > default with an env-var test;
  `--install` prints the apply pair with a bootout-before-bootstrap test (Rock 4).
- All proofs stop the console by pid (`kill %1` inside a subshell is a no-op in zsh);
  Rock 3 checks the port is released.
- Cloudflare runbook probes locally first and through `cloudflared access curl`;
  `--behind-proxy` is added only when the probe shows the name under `forwarded_host`;
  TCC troubleshooting note for checkouts under `~/Documents` (Rock 5).
````

### VTO.md as reviewed

````markdown
# VTO — Hosted DCA console

Vision/Traction document for the `rocket-fuel/hosted-console` cycle (Kickoff, 2026-09-13).

## What

The existing local DCA operations console (`from.py` → `dca_console.py`, stdlib
`ThreadingHTTPServer`, browser assets under `console/`) running unattended on the Mac
that holds the artifact tree, reachable over HTTPS from the operator's phone or laptop
anywhere, with the same read-mostly surface it has today: daily run ledger, confirmed
spend, schedule countdowns, bounded demo/test jobs, and Wondrous Octopus forecasts when an
Octopus run directory is configured for the service (`--octopus-run`).

## Why now

The console exists, is tested (78 tests) and hardened, but it dies with the terminal
that launched it and binds only to loopback. The operator wants to see what the seven
scheduled streams did, what was spent, and what fires next without sitting at the desk.

## Who

A single operator (the repository owner). Multi-user access and roles are deferred.

## Success criteria for this cycle

1. A launchd login agent starts the console at login in `--local --no-browser` mode
   against the real `cron_tracking` tree, restarts it on failure, and stops it cleanly
   on SIGTERM (`Stopping local console.` in the log, exit 0).
2. From another device on the home LAN, `curl` gets `401` without credentials and `200`
   with them; a phone browser prompts once and renders real runs.
3. **Code-complete** means: LAN acceptance demonstrated from a second device in-cycle,
   the launchd cycle demonstrated, and a deployment runbook for each front (Tailscale
   Serve, Cloudflare Tunnel + Access) with an operator acceptance checklist. **Deployed**
   means the operator has installed and logged in to one front (their step; the Visionary
   cannot create accounts or log in) and completed that checklist with a phone loading
   the console over HTTPS off the home wifi. Same code path for both fronts; only the
   runbook differs.
4. All existing console tests plus the new ones pass; `ruff check .` is clean; the full
   core suite is green; the demo smoke run still works.
5. `CLAUDE.md` and `docs/repo_map.md` document the console for the first time.

## Non-negotiables

- Standard library only at runtime. No new third-party dependency, no build step.
- The console never places, reviews, cancels or schedules brokerage orders. Hosting it
  changes nothing about that; the allowlisted actions stay demo/test/report only.
- Default bind stays `127.0.0.1`. Any non-loopback bind or any external hostname
  **refuses to start** without a credential file (fail closed, enforced when the access
  policy object is constructed, so no caller can open a socket around it).
- The persistent launchd service is always loopback-bound; a LAN bind is a foreground,
  build/test-only command over plaintext HTTP with a disposable credential that is rotated
  before deployment.
- The credential never appears in argv, the environment, job logs, `/api/bootstrap`,
  snapshots, summaries or the rendered launchd plist. Anything that could echo it goes
  through `dca_console_jobs.redact`.
- No 6-12 digit literal anywhere in tracked files: the account-number scan in
  `tests/test_config_invariants.py` covers every `git ls-files` entry, `deploy/` included.
- No hard-coded absolute paths; output and log locations come from
  `dca_paths.stream_root('console')`.
- `X-Forwarded-*` headers are honoured only with an explicit opt-in, and only when the
  server is bound to loopback (the proxy is on this machine).
- Match the house comment style: explain *why* a line exists and cite the review that
  motivated it.

## Out of scope this cycle

New panels (3D market graph route, weekly dashboard, alert digest), brute-force backoff,
proxy identity display, multi-user roles, IPv6 or hostname binds, in-process TLS. All
tracked in `DEFERRED.md`.
````

### Round 2 — invocation note

`codex exec resume` on CLI 0.145.0 rejects `--sandbox` ("unexpected argument"). The
first round-2 attempt exited 2 before contacting the model. Read-only is enforced for the
resumed session with the equivalent config override `-c 'sandbox_mode="read-only"'`
(the session was also created read-only); no other change.

### Round 2 — Codex reply

Timestamp: 2026-09-13T05:40:22Z

> I’ll re-audit Revision 3 from scratch, then map every round-1 title to either RESOLVED or OPEN and separately identify only material new gaps. I’ll also verify the revised proof mechanics and deployment assumptions against the existing code and CI configuration.
> The revised plan closes most of the original design gaps. I’m now checking whether its acceptance commands actually run under the shell it explicitly targets, and whether “rolling today” reaches job submission—not just the displayed snapshot.
> A substantive regression surfaced in the rolling-date fix: as written, every local-mode `snapshot()` ignores `Settings.day`, so explicit `--local --date …` stops working and existing tests that pin a local day fail. The plan needs to distinguish an unpinned hosted service from an explicitly pinned date.
> Revision 3 closes 14 round-1 findings; four remain open, plus three new material findings.
>
> [BLOCKER] credential-in-curl-argv — RESOLVED: authenticated proofs now pass curl a `/dev/fd` config path without placing the credential in argv.
>
> [BLOCKER] fail-closed-only-at-cli — RESOLVED: `ConsoleAccess.__post_init__` owns the complete exposure matrix before any server can be constructed.
>
> [MAJOR] plaintext-lan-basic-auth — RESOLVED AS AN EXPLICIT DEFER: Phase A is foreground-only, plainly warned, disposable, and followed by mandatory rotation, while TLS is recorded in DEFERRED.md.
>
> [MAJOR] cross-site-get-exception-writes — RESOLVED: only the side-effect-free document route receives navigation tolerance, with API non-invocation and no-write assertions.
>
> [MINOR] origin-not-bound-to-host — RESOLVED: Origin must match the selected effective authority, including proxy and alias-mismatch tests.
>
> [MAJOR] auth-file-confidentiality-unproved — STILL OPEN: pathname `lstat` followed by an unspecified read does not prove the bytes came from the checked inode, allowing a swap in a writable parent to bypass the checks.
>
> [MAJOR] hosted-default-date-freezes — STILL OPEN: making every local `default_day()` use wall-clock today discards explicit `--local --date`, breaking the existing pinned-date test and direct local `snapshot()` semantics.
>
> [MAJOR] restart-breaks-job-buttons — RESOLVED: one 403-specific re-bootstrap and retry is safe because the current POST path rejects before `submit()`.
>
> [MAJOR] launchd-drops-octopus-input — RESOLVED: the renderer passes and tests `--octopus-run`, while VTO conditions the forecast promise on its configuration.
>
> [MAJOR] kickstart-does-not-reload-plist — RESOLVED: changed definitions now use `bootout` followed by `bootstrap`, reserving `kickstart` for unchanged plists.
>
> [MAJOR] custom-root-splits-service-output — RESOLVED: one precedence-resolved root supplies both the service environment and log paths.
>
> [MINOR] custom-label-is-an-unsafe-target — RESOLVED BY REMOVAL: the option is gone and the launchd label is fixed.
>
> [MAJOR] verification-omits-coverage-gate — RESOLVED: whole-cycle verification includes CI’s full `--cov-fail-under=90` command.
>
> [MINOR] focused-tests-variable-is-never-set — STILL OPEN: it is now set as a scalar, but zsh does not word-split `$CONSOLE_TESTS`, so every focused pytest proof receives one invalid space-containing path.
>
> [MINOR] new-files-escape-local-literal-scan — RESOLVED: intent-to-add occurs before invariant scans once the new files exist.
>
> [MAJOR] cloudflared-ingress-is-incomplete — RESOLVED: the terminal `http_status:404` rule and ingress validation are both required.
>
> [MINOR] renderer-cannot-produce-phase-a — RESOLVED: the renderer intentionally has no `--bind` and claims only the loopback Phase B form.
>
> [MAJOR] hosted-success-is-not-gated — STILL OPEN: the generic checklist uses plain curl where Cloudflare Access intercepts it, cannot reliably expose `forwarded_host` on a successful loopback-Host probe, and unconditionally enables `--behind-proxy` contrary to both conditional runbooks.
>
> [MAJOR] auth-failure-logger-is-not-fail-closed — OPEN: truncating before redaction can expose credential fragments, malformed targets can escape into unrate-limited tracebacks, and no synchronized limiter or concurrency regression is required for the threaded server.
>
> [MAJOR] sigterm-handler-installed-after-readiness — OPEN: scoping the handler around `serve_forever` leaves the flushed startup banner before protection, so the planned immediate post-banner SIGTERM can still terminate with `-15`.
>
> [MAJOR] cloudflared-access-curl-url-order — OPEN: PLAN.md places `-si` before the URL, but [cloudflared’s current command definition](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/access/cmd.go) parses the first argument as the application URL.
>
> credential-in-curl-argv — ACCEPT: no further plan change is needed.
>
> fail-closed-only-at-cli — ACCEPT: construction-time enforcement is the correct boundary.
>
> plaintext-lan-basic-auth — DEFER: retain the stated safeguards and the explicit TLS backlog item.
>
> cross-site-get-exception-writes — ACCEPT: the revised route boundary and regressions are sufficient.
>
> origin-not-bound-to-host — ACCEPT: the effective-authority rule closes the mismatch.
>
> auth-file-confidentiality-unproved — FIX: require `O_NOFOLLOW` opening, `fstat` validation, bounded reading through that same descriptor, and a swap regression.
>
> hosted-default-date-freezes — FIX: distinguish an unpinned hosted service from an explicit or fixture-pinned date and test both behaviors.
>
> restart-breaks-job-buttons — ACCEPT: the bounded retry and manual restart proof are appropriate.
>
> launchd-drops-octopus-input — ACCEPT: passthrough and narrowed success language align.
>
> kickstart-does-not-reload-plist — ACCEPT: the apply sequence is now correct.
>
> custom-root-splits-service-output — ACCEPT: root resolution and tests cover both override sources.
>
> custom-label-is-an-unsafe-target — ACCEPT: the prior KILL has already been applied.
>
> verification-omits-coverage-gate — ACCEPT: the exact gate is present.
>
> focused-tests-variable-is-never-set — FIX: make `CONSOLE_TESTS` a zsh array and invoke pytest with `"${CONSOLE_TESTS[@]}"`.
>
> new-files-escape-local-literal-scan — ACCEPT: the new-file visibility gap is closed.
>
> cloudflared-ingress-is-incomplete — ACCEPT: the required terminal rule and validation are covered.
>
> renderer-cannot-produce-phase-a — ACCEPT: foreground Phase A and rendered Phase B are now honestly separated.
>
> hosted-success-is-not-gated — FIX: make acceptance front-specific, use Access-aware probing, select `--behind-proxy` only from reliable diagnostics, and explicitly perform the second-device acceptance.
>
> auth-failure-logger-is-not-fail-closed — FIX: require exception-safe extraction, redact-before-truncate, control-character normalization, locked limiter state, and cutoff/malformed/concurrent regressions.
>
> sigterm-handler-installed-after-readiness — FIX: require the SIGTERM context to be active before emitting any readiness banner.
>
> cloudflared-access-curl-url-order — FIX: specify `cloudflared access curl https://… -si`.
>
> VERDICT: BLOCKED — 7 open finding(s)

### Round 2 — Discuss (Visionary responses, on the record)

14 round-1 findings confirmed resolved (ACCEPT/DEFER as Codex stated; no disagreement).
Open items:

| # | Finding | Codex | Visionary response |
| --- | --- | --- | --- |
| 6 | auth-file-confidentiality-unproved | FIX | **Agree.** `lstat`-then-read is a check/use race. `load_auth_file` now opens with `os.open(path, O_RDONLY \| O_NOFOLLOW \| O_CLOEXEC)` (a symlink fails with `ELOOP`), validates with `os.fstat` on that descriptor (regular file, owner, mode, size ≤ 4096) and reads through the same descriptor, so the bytes provably come from the checked inode. Test asserts `fstat`/`read` use the fd returned by `open` and that no path-based read happens; symlink and oversized-file cases added. |
| 7 | hosted-default-date-freezes | FIX | **Agree.** `Settings.day` becomes `date \| None`: `None` = unpinned (local mode without `--date`) → `default_day()` is today (ET) at request time; an explicit `--date` or a fixture-provided day pins it → `default_day()` returns it. Demo mode always has a concrete day. Both behaviours tested; existing pinned-day tests keep their semantics. |
| 14 | focused-tests-variable-is-never-set | FIX | **Agree.** zsh does not word-split `$CONSOLE_TESTS`. Every proof now uses a zsh/bash array: `CONSOLE_TESTS=(…)` and `pytest "${CONSOLE_TESTS[@]}"`. |
| 18 | hosted-success-is-not-gated | FIX | **Agree.** The acceptance checklist is now front-specific (Tailscale: plain curl from a tailnet device; Cloudflare: `cloudflared access curl <url> -si`), adds `--behind-proxy` only when the 403 body shows the name under `forwarded_host` and not `received_host`, and has an explicit second-device step (phone on cellular, off the home wifi). |
| 19 | auth-failure-logger-is-not-fail-closed (new) | FIX | **Agree.** The line is built exception-safely (`urlsplit` inside `try/except`; a malformed target logs the fixed text `malformed-target`), control characters are stripped before anything else, **redaction happens before truncation** to 128 characters, and the limiter state is lock-protected. Regressions: credential straddling the cutoff, `\r\n` in the path, malformed target, 20-thread concurrent burst. |
| 20 | sigterm-handler-installed-after-readiness (new) | FIX | **Agree.** The SIGTERM context is entered before the readiness banner is printed and covers server creation through `serve_forever`; the subprocess test (SIGTERM immediately after the banner) is the regression. |
| 21 | cloudflared-access-curl-url-order (new) | FIX | **Agree.** `cloudflared access curl https://console.<domain>/api/health -si` (URL first). |

Solve: PLAN.md revised to revision 4 (see "Changes since round 2" at its end).

## Round 3 — revised plan submitted

Timestamp: 2026-09-13T05:43:28Z
PLAN.md sha256: 720f5babdb0dd9c2…  (VTO.md, DEFERRED.md unchanged since round 2)
Invocation: `codex exec resume <session> -m gpt-5.6-sol -c 'sandbox_mode="read-only"' --json`

### Prompt sent (Round 3)

> Round 3. PLAN.md is revised to "Revision 4" addressing the seven items you left open in
> round 2; VTO.md and DEFERRED.md are unchanged since round 2. Re-read PLAN.md end to end;
> the "Changes since round 2" section at the end is the index. You remain read-only.
>
> Resolutions for the seven open findings:
>
> - auth-file-confidentiality-unproved — `load_auth_file` opens with
>   `os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)`, validates with `os.fstat` on that
>   descriptor (regular file, owner, mode, size ≤ 4096) and reads through the same descriptor
>   with a bounded `os.read`; tests assert `fstat`/`read` use the fd `open` returned and no
>   path-based read occurs, plus symlink (ELOOP) and oversized-file cases.
> - hosted-default-date-freezes — `Settings.day: date | None`; `None` (local mode without
>   `--date`) is unpinned and `default_day()` returns today in ET at request time; an
>   explicit `--date` or a fixture-provided day pins it; demo mode always concrete. Tests
>   cover both, and `main()` with/without `--date`.
> - focused-tests-variable-is-never-set — `CONSOLE_TESTS=(…)` array, expanded as
>   `"${CONSOLE_TESTS[@]}"` in every proof.
> - hosted-success-is-not-gated — the acceptance checklist is now two front-specific
>   checklists (Tailscale plain curl from a tailnet device; Cloudflare
>   `cloudflared access curl <url> -si`), `--behind-proxy` is chosen from the probe body
>   (name under `forwarded_host` and not `received_host`), and a cellular second-device step
>   is explicit.
> - auth-failure-logger-is-not-fail-closed — exception-safe extraction (`malformed-target`
>   fixed text), control characters stripped, redact **then** truncate to 128, lock-protected
>   limiter; regressions for a credential straddling the cutoff, `\r\n` in the path, a
>   malformed target, and a 20-thread concurrent burst.
> - sigterm-handler-installed-after-readiness — the SIGTERM context is entered before the
>   readiness banner and covers server creation through `serve_forever`.
> - cloudflared-access-curl-url-order — `cloudflared access curl https://… -si`.
>
> For each of those seven, confirm resolved or still open using the same [SEVERITY]
> short-title label. New findings are allowed but should be rare this late — flag anything
> major. End with the same single verdict line as before, as the literal last line:
> `VERDICT: SAME PAGE` if you have zero open FIX or KILL items, otherwise
> `VERDICT: BLOCKED — N open finding(s)`.

### PLAN.md revision 4 — sections changed since round 2

````markdown
## Design decisions

| Topic | Decision | Why |
|---|---|---|
| Secret source | `--auth-file PATH`: one line `user:password`; POSIX only (`os.name == "posix"`, otherwise `ValueError` — fail closed, no Windows tolerance for this feature); opened with `os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)` (a symlink fails with `ELOOP`), validated with `os.fstat` **on that descriptor** (`S_ISREG`; `st_uid == os.getuid()`; `st_mode & 0o077 == 0`; `st_size ≤ 4096`) and read through the same descriptor with a bounded `os.read`, so the bytes provably come from the checked inode; username printable ASCII without `:`; password ≥ 12 chars. Errors name the rule, never the contents | An env var would be copied into every job child by `env = dict(os.environ)` (`dca_console.py:237`), show in `launchctl print` / `ps -E`, and sit inside the plist. A path in argv leaks nothing. Ownership and regular-file checks are what make "0600" mean something, and checking a path then reading it separately is a check/use race a writable parent directory could exploit (Same Page round 2) |
| Fail-closed construction | All access rules are enforced in `ConsoleAccess.__post_init__`, so an invalid combination cannot be constructed anywhere — not in `main()`, not by a future caller of `make_server()`. `main()` maps `ValueError` → `parser.error` (exit 2) | A CLI-only check would let another entry point open a reachable socket without a credential |
| Auth scheme | HTTP Basic on every route except `/api/health`, only when a credential is configured; `secrets.compare_digest` on `b"user:password"`; failure → `401` + `WWW-Authenticate: Basic realm="DCA console", charset="UTF-8"` + JSON error | Native browser prompt on a phone; zero frontend change for auth because same-origin `fetch` (`console/app.js:381,384,411`) and the `<a download>` export (`console/index.html:38`) attach credentials automatically |
| `/api/health` unauthenticated | Still behind the Host/Origin check; body is only `{"status","mode"}` | Lets probes and the launchd runbook work without embedding the password anywhere |
| `X-Console-Token` | Keep, plus a frontend re-bootstrap: on a 403 from `POST /api/jobs` the page clears `state.token`, fetches `/api/bootstrap` once and retries once | Basic credentials are ambient like cookies; the token (fetched same-origin, no CORS headers) is the CSRF defence. A launchd restart rotates it; `do_POST` checks the token before `submit()` (`dca_console.py:390`), so a 403 proves the job was never accepted and one retry is safe |
| Rolling default day | `Settings.day` becomes `date \| None`. `None` means *unpinned* (local mode started without `--date`): `ConsoleApp.default_day()` returns `datetime.now(ET).date()` at request time. An explicit `--date`, or a fixture-provided day, *pins* it: `default_day()` returns `settings.day`. Demo mode always has a concrete day (`default_demo_day()`). `default_day()` feeds `/api/bootstrap`, the `snapshot()` default and `default_date` | `main()` freezes `Settings.day` at startup (442); a service that survives midnight would otherwise open new tabs on stale data. `--local --date …` and the existing pinned-day tests must keep their meaning (Same Page round 2) |
| `--bind ADDR` | IPv4 literal only (`ipaddress.IPv4Address`), default `127.0.0.1`; foreground use only — the launchd renderer has no `--bind`. A non-loopback bind makes the startup banner (pure `startup_banner(access, port)`) print `plain HTTP: the Basic credential and console token are visible to anyone on this network` | `HTTPServer` is AF_INET; hostnames/IPv6 deferred; the persistent service stays loopback-only; the operator must see the plaintext caveat where they start the process |
| `--trusted-host HOST[:PORT]` (repeatable) | lowercased; hostname regex; no scheme, path, wildcard or whitespace; port ≤ 65535 | Extra names accepted in `Host`. Also accept `https://` origins (today only `http://`, `dca_console.py:344`, which would block POSTs behind an HTTPS proxy) |
| `hosts(port)` | `{127.0.0.1:port, localhost:port}` ∪ `{bind:port}` when bind is a specific non-loopback IP ∪ `trusted_hosts`; `0.0.0.0` adds nothing | Phase A needs no `--trusted-host`; with `0.0.0.0` the server cannot know which address the client typed |
| Effective authority and `Origin` | The *effective authority* is the accepted `Host`, or under `--behind-proxy` the accepted `X-Forwarded-Host`. When `Origin` is present it must equal exactly `http://<authority>` or `https://<authority>` | Membership of both in allowed sets is not enough: a loopback `Host` with a tailnet `Origin` (or vice versa) must be refused |
| `--behind-proxy` | Additionally accept a **configured** name (never a loopback name) from `X-Forwarded-Host`; requires ≥1 `--trusted-host` **and** a loopback bind | A LAN-reachable socket that honours forwarded headers would let any LAN client forge them |
| Fail-closed matrix (in `__post_init__`) | non-loopback bind without a credential; any `trusted_hosts` without a credential; `behind_proxy` without `trusted_hosts` or with a non-loopback bind; `0.0.0.0` without `trusted_hosts`; malformed bind / host values | Nothing listens on a reachable address without a credential |
| `Sec-Fetch-Site: cross-site` | Rejected everywhere **except** `GET /` with `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. Every `/api/*` route, including GETs, stays cross-site-rejected | Cloudflare Access login is a redirect chain through `<team>.cloudflareaccess.com`; Fetch Metadata computes the site over the whole chain, so the first document load after login arrives `cross-site`. The document route is side-effect-free; `/api/snapshot` and `/api/export` are not (they call `_save_summary()`), so they keep the strict rule. Existing test at `tests/test_dca_console.py:122-124` still gets 403 |
| 403 body | adds `received_host` (≤ 253 chars) and `forwarded_host` | The runbook's "what does the backend see" probe; JSON with `nosniff`, no HTML |
| Brute-force backoff | Deferred (see `DEFERRED.md`); 12-char minimum, documented generator | Behind either proxy every request is from `127.0.0.1`; per-IP state becomes a global lockout lever |
| Auth-failure log line | One fixed-shape stderr line per failure, built exception-safely: the route comes from `urlsplit(self.path).path` inside `try/except` (a malformed target logs the fixed text `malformed-target`); everything outside printable ASCII is stripped first; then `redact()`; **then** truncation to 128 characters; prefixed with client address and method. A lock-protected limiter emits at most one line per 5 s per process and reports the suppressed count on the next emitted line | Any unauthenticated LAN/tailnet client can hit 401/403 at line rate; the line must not let it grow `launchd.err.log` unboundedly, inject newlines, or plant a credential-shaped string. Truncating before redaction could leave a fragment the regex no longer recognises; the server is threaded, so limiter state needs a lock (Same Page round 2) |
| SIGTERM | Context manager installs a handler that raises `KeyboardInterrupt`, reusing the Ctrl+C teardown (`dca_console.py:492-497`); restores the previous handler; entered **before** the readiness banner is printed and covering server creation through `serve_forever` (the serving branch only, not `--once`/`--run`/`--check`) | `server.shutdown()` from the handler would deadlock (same thread as `serve_forever`). A handler installed after the banner would let a SIGTERM sent on seeing the banner hit the default action and exit `-15` (Same Page round 2). Worst-case teardown ≈ 11 s (`dca_console_jobs.py:210-226`), inside the plist's `ExitTimeOut = 30` |
| Handler hygiene | `Handler.timeout = HANDLER_TIMEOUT_SECONDS` (30; today `None`, only POST bodies get `settimeout(5)` at line 400); `version_string()` override | An idle LAN/proxy connection must not pin a thread; `Server:` should not advertise the Python version |
| Long uptime | Cap `ConsoleApp.signatures` to the last 32 days | Everything else is already bounded |
| launchd | `deploy/launchd/render_console_agent.py` renders the plist with `plistlib` (`--print` / `--install`), validates the auth file via `load_auth_file`, takes `--python PATH` (default `<checkout>/.venv/bin/python`; refuses when the chosen interpreter does not exist), resolves **one** artifact root with precedence `--cron-tracking-root` > `$DCA_CRON_TRACKING_ROOT` > `dca_paths` default, always writes it into the plist environment and derives the log paths from that same value, creates the log directory, prints the `launchctl` commands including the apply pair (`bootout … 2>/dev/null || true`, then `bootstrap`). Fixed label `com.all-stock-dca.console`. Optional `--octopus-run DIR` passthrough. No `--bind`. No placeholder template | Same "logic in a script, not the unit" rationale as `deploy/run-drift-check.sh:6-17`; `plistlib` cannot mis-escape; one resolved root means logs and console artifacts can never land in different trees; a fixed label removes an unvalidated path/shell fragment |
| pyproject | No change | Console is checkout-only; `deploy/` is not shipped |
| Proxy `Host` semantics (read from source at HEAD, not a live install — uncertainty acknowledged) | Tailscale Serve preserves `Host` and adds `X-Forwarded-Host/-Proto/-For` + `Tailscale-User-*`; cloudflared preserves `Host` unless `httpHostHeader` is set | Accept the configured name in `Host` or (with `--behind-proxy`) `X-Forwarded-Host`; every runbook starts with a `curl -si …/api/health` whose 403 body prints what arrived |
| Credentials in proofs | `curl -K <(printf 'user = "%s"\n' "$(<"$AUTH")")` | The curl config arrives through a `/dev/fd` path; argv never holds the secret |

### Rock 1 — Basic auth from a 0600 file (loopback-safe, no exposure yet)

**Scope.** Frozen `ConsoleAccess` dataclass beside `Settings` (`username: str = ""`,
`credential: bytes | None = field(default=None, repr=False)`) with `__post_init__`
validation of the credential shape; `load_auth_file(path) -> tuple[str, bytes]` with the
rules above (POSIX only, `O_NOFOLLOW` open, `fstat` on the descriptor, regular file, owner,
mode, size ≤ 4096, bounded read through that same descriptor, one line, `:`), error
messages naming the rule but never the contents; `ConsoleAccess.authorized(headers)` (`Basic <b64>`,
scheme case-insensitive, `base64.b64decode(..., validate=True)`, `compare_digest`);
`--auth-file`; 401 path on every route except `/api/health` — order `trusted` → 403,
`authorized` → 401, then token → 403 on POST; `dca_console_jobs.register_secret()`
consulted by `redact()`; the exception-safe, control-stripped, redact-then-truncate,
lock-limited auth-failure line from the design table. `make_server(app, port, access=None)`
keeps today's behaviour by default so all 78 existing tests pass untouched. Frontend untouched.

**Non-goals.** `--bind`, trusted hosts, proxy headers (Rock 2); backoff; UI hint that
auth is on.

**Files.** `dca_console.py`, `dca_console_jobs.py`, `tests/test_dca_console.py`.

**Tests.** `test_auth_file_rules_fail_closed` (missing, mode 0644, symlink to a good file
(refused by `O_NOFOLLOW`), FIFO, directory, a file larger than 4096 bytes, wrong owner via
monkeypatched `os.getuid`, non-POSIX via monkeypatched `os.name`, no colon, empty, two
lines, 11-char password → `ValueError` whose message lacks the file contents; good file →
`("dca", b"dca:s3cret-pw-abcdefg")`); `test_auth_file_is_read_from_the_checked_descriptor`
(monkeypatched `os.open`/`os.fstat`/`os.read` record the descriptor: `fstat` and `read`
receive the fd `open` returned, and no path-based `open()`/`Path.read_*` happens);
`test_basic_auth_required_on_every_route_except_health` (every asset, `/api/bootstrap`,
`/api/snapshot`, `/api/export`, `/api/docs` → 401 with `WWW-Authenticate` starting
`Basic realm=`; `/api/health` → 200; POST with right token but no credentials → 401 and
`app.submit` never called); `test_basic_auth_wrong_credentials_are_rejected` (wrong
password, wrong user, undecodable base64, `Bearer` scheme → 401);
`test_basic_auth_correct_credentials_pass_and_token_still_required` (bootstrap 200; POST
with credentials but no token → 403; with both → 202);
`test_password_never_appears_in_responses_or_redaction` (bodies/headers lack it; `redact()`
masks it; `repr(access)` lacks it); `test_auth_failure_is_logged_without_credentials`;
`test_auth_failure_log_line_is_bounded_and_redacted` (a 60 KB request path and a path
containing the fixture password each yield one short line with the route only and the
password masked; a password straddling the 128-character cutoff is still masked; a path
carrying `\r\n` and other control characters yields exactly one line; a malformed request
target yields the fixed `malformed-target` text and no traceback; a burst of 50 failures
yields at most two lines plus a suppressed count); `test_auth_failure_limiter_is_thread_safe`
(20 threads failing concurrently → at most two lines and a consistent suppressed count).

**Proof.**
```bash
CONSOLE_TESTS=(tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py); AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest "${CONSOLE_TESTS[@]}" tests/test_config_invariants.py -q \

.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done
curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/health
curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/bootstrap
curl -s -o /dev/null -w '%{http_code}\n' -K <(printf 'user = "%s"\n' "$(<"$AUTH")") http://127.0.0.1:8765/api/bootstrap
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
```
Expected: ruff clean; 87 console passes (78 + 9) and the invariants file green; curl
prints `200 401 200`; then `Stopping local console.` and `exit=0`.

### Rock 3 — Clean SIGTERM, restart resilience, long-uptime bounds

**Scope.** `_sigterm_as_keyboard_interrupt()` context manager entered before the
readiness banner (472-473) and covering server creation through `serve_forever` (491),
restoring the previous handler, serving branch only; `Settings.day: date | None` with
`main()` passing `args.date` (None when absent in local mode) and `ConsoleApp.default_day()`
(unpinned → today in ET at request time; pinned → `settings.day`) used by `/api/bootstrap`,
`snapshot()`, `_run` and `default_date`; cap `signatures` (129, 225) to the last 32 days; `console/app.js`:
on a 403 from `POST /api/jobs`, clear `state.token`, re-bootstrap once, retry once, then
surface the error.

**Non-goals.** SIGHUP reload; job-file pruning; log rotation.

**Files.** `dca_console.py`, `console/app.js`, `tests/test_dca_console.py`.

**Tests.** `test_sigterm_stops_console_cleanly` (posix-only subprocess: spawn
`from.py --local --no-browser --port 0 --root <tmp> --output-dir <tmp>`, wait for the
`DCA console (local)` banner, `send_signal(SIGTERM)`, `wait(timeout=20)` → returncode 0
and stdout contains `Stopping local console.`);
`test_sigterm_handler_raises_and_restores_previous_handler`;
`test_unpinned_local_default_day_rolls_over_midnight` (`Settings(day=None)`, monkeypatched
clock: bootstrap `default_date` and `snapshot()["daily"]["date"]` change from one ET day to
the next without a restart); `test_explicit_date_stays_pinned` (local mode with a concrete
`day`, and demo mode, keep `default_date` and the snapshot default on that day across the
same clock change; `main(["--local", "--date", …])` produces a pinned `Settings`, and
without `--date` an unpinned one);
`test_summary_signature_cache_is_bounded` (100 distinct days → `len(app.signatures) <= 32`).

**Proof.**
```bash
CONSOLE_TESTS=(tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py); AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest "${CONSOLE_TESTS[@]}" -q \

.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
! lsof -nP -iTCP:8765 -sTCP:LISTEN && echo "port released"
```
Expected: 103 passes; `Stopping local console.` then `exit=0` within seconds, then
`port released`. Manual (after Rock 4): with a tab open, `launchctl kickstart -k
gui/$(id -u)/com.all-stock-dca.console`, then click "Test DCA pipeline" in the same tab
without reloading → the job starts.

## Operator acceptance checklist (post-merge; the cycle is *deployed* only when one front passes)

Common first step: rotate the credential file (regenerate it with the one-liner above; the
Phase A value crossed the LAN in plaintext).

**Tailscale Serve**
1. Install Tailscale on this Mac and the phone; log in (operator step). `tailscale serve
   --bg http://127.0.0.1:8765`; `tailscale serve status` shows the tailnet URL and no Funnel.
2. Probe from a tailnet device (laptop on the tailnet, or the phone):
   `curl -si https://<mac>.<tailnet>.ts.net/api/health`. Expect a 403 JSON body. Read it:
   if the name appears under `received_host`, no `--behind-proxy` is needed; only if it
   appears under `forwarded_host` and `received_host` is something else, add `--behind-proxy`.
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
   (a plain `curl` is answered at the edge). Expect a 403 JSON body; choose `--behind-proxy`
   by the same `received_host` / `forwarded_host` rule as above.
4. Re-render with `--trusted-host console.<domain> [--behind-proxy] --install`; run the
   printed apply pair; `launchctl print` shows `state = running`. Repeat the probe: expect 200.
5. Second-device acceptance: phone on cellular, wifi off: `https://console.<domain>/`
   completes the Access login, then prompts for Basic once, shows `LOCAL` mode with the
   latest scheduled runs, "Export JSON" downloads, a job button starts a job. Confirm the
   Access policy is attached to the hostname before leaving it running.

## Changes since round 2 (Same Page Meeting)

- auth-file-confidentiality-unproved → `O_NOFOLLOW` open, `fstat` on the descriptor,
  bounded read through it; descriptor test, symlink and oversized-file cases (Rock 1).
- hosted-default-date-freezes → `Settings.day: date | None`; unpinned local mode rolls,
  an explicit `--date` or fixture day stays pinned; both tested (Rock 3).
- focused-tests-variable-is-never-set → `CONSOLE_TESTS` is an array expanded as
  `"${CONSOLE_TESTS[@]}"` (all proofs).
- hosted-success-is-not-gated → front-specific acceptance checklists, Access-aware probe,
  `--behind-proxy` chosen from the probe body, explicit cellular second-device step.
- auth-failure-logger-is-not-fail-closed → exception-safe extraction, control characters
  stripped, redact before truncate, lock-protected limiter; cutoff / malformed / concurrent
  regressions (Rock 1).
- sigterm-handler-installed-after-readiness → the SIGTERM context is entered before the
  banner is printed (Rock 3).
- cloudflared-access-curl-url-order → URL first, flags after (Rock 5, checklist).

````

### Round 3 — Codex reply

Timestamp: 2026-09-13T07:23:03Z

> I’ll audit Revision 4 end to end, then test each of the seven resolutions against the existing call paths and the plan’s exact proof commands. I’ll treat VTO/DEFERRED as unchanged context and report only unresolved items or genuinely material new contradictions.
> The same-fd credential design is now sound against path swaps, but one fail-closed edge remains under scrutiny: opening a FIFO with blocking `O_RDONLY` happens before `fstat`, so the stated FIFO rejection test may hang unless the open itself is nonblocking.
> Six prior items look closed. I’m doing one final consistency pass on the proxy probe and the two newly surfaced edge cases before fixing the verdict count.
> [MAJOR] auth-file-confidentiality-unproved — RESOLVED: `O_NOFOLLOW`, same-descriptor `fstat`, and bounded descriptor reads close the pathname substitution race, with tests enforcing descriptor identity.
>
> [MAJOR] hosted-default-date-freezes — RESOLVED: unpinned local settings now compute the ET day per request while explicit dates and demo mode remain pinned.
>
> [MINOR] focused-tests-variable-is-never-set — RESOLVED: every proof defines `CONSOLE_TESTS` as an array and expands it with `"${CONSOLE_TESTS[@]}"`.
>
> [MAJOR] hosted-success-is-not-gated — STILL OPEN: if a front rewrites `Host` to an accepted loopback authority and places the public name only in `X-Forwarded-Host`, the initial health probe returns 200 without diagnostic fields, leaving the checklist unable to select `--behind-proxy`, whose forwarded-host precedence is also unspecified and untested for this fallback shape.
>
> [MAJOR] auth-failure-logger-is-not-fail-closed — RESOLVED: extraction is exception-safe, control bytes are removed, redaction precedes truncation, and locked rate limiting has adversarial regressions.
>
> [MAJOR] sigterm-handler-installed-after-readiness — RESOLVED: the handler now covers server construction, readiness publication, and `serve_forever`.
>
> [MAJOR] cloudflared-access-curl-url-order — RESOLVED: both occurrences place the URL before forwarded curl flags.
>
> [MAJOR] fifo-auth-file-blocks-before-validation — `os.open` with blocking `O_RDONLY` waits for a FIFO writer before `fstat` can reject it, so startup and the required FIFO regression can hang indefinitely.
>
> [MAJOR] rolling-default-breaks-demo-pipeline — applying the unpinned calendar-day `default_day()` to `_run` replaces the existing recent-full-session selection, causing “Test DCA pipeline” to produce no `run_state.json` on weekends, holidays, and early-close days.
>
> Verdicts:
>
> - auth-file-confidentiality-unproved — ACCEPT: the original confidentiality race is closed.
> - hosted-default-date-freezes — ACCEPT: rollover and pinning semantics are now explicit and tested.
> - focused-tests-variable-is-never-set — ACCEPT: the proof commands now expand correctly.
> - hosted-success-is-not-gated — FIX: force the initial probe into a diagnostic 403, define configured forwarded-host precedence, and test loopback `Host` plus external forwarded host and matching `Origin`.
> - auth-failure-logger-is-not-fail-closed — ACCEPT: the failure path is bounded, redacted, and concurrency-safe.
> - sigterm-handler-installed-after-readiness — ACCEPT: the readiness race is closed.
> - cloudflared-access-curl-url-order — ACCEPT: the invocation order is corrected.
> - fifo-auth-file-blocks-before-validation — FIX: add `O_NONBLOCK` and make the FIFO regression timeout-bounded.
> - rolling-default-breaks-demo-pipeline — FIX: use an explicit pinned date when supplied, otherwise retain `default_demo_day()` for `_run`, with a closed-market-day regression.
>
> VERDICT: BLOCKED — 3 open finding(s)

### Round 3 (addendum) — Visionary's second adversarial panel on revision 4

Claude workflow `plan-pressure-test-r4`: three lens reviewers, 9 raw findings, three
skeptics each (three verifier agents were lost to the operator's session limit; the two
findings they touched are marked under-verified). 7 survived, 2 did not:

| # | Finding (lens, votes) | Response |
| --- | --- | --- |
| P1/P3/P6 | FIFO auth file hangs the `O_RDONLY` open before `fstat` runs (security + correctness + operability, 3/3 each, reproduced on this Mac) | **Agree, FIX.** Same as Codex's `fifo-auth-file-blocks-before-validation`. `O_NONBLOCK` added; FIFO test via `os.mkfifo` with a bounded-time assertion; descriptor test asserts `O_NONBLOCK` is among the recorded flags. |
| P2/P5/P7 | Rock 4's "default interpreter missing → exit 2" case cannot pass on the Mac where `.venv` exists (3/3, 2/3) | **Agree, FIX.** Renderer exposes module-level `DEFAULT_PYTHON` read at call time; the test monkeypatches it to a missing path; positive counterpart patched to `sys.executable`. |
| P4 | A process-wide 5-second limiter makes the Rock 1 log tests order-dependent and unpassable as written (correctness, 3/3) | **Agree, FIX.** `AuthFailureLog(interval, clock, stream)` is created per `make_server` call (injectable via `failure_log=`), never module-global; per-line tests use `interval=0` or a fake clock; burst/thread tests use a fresh instance with the real interval; no test relies on wall-clock spacing. |
| R1 | `X-Forwarded-Host` ignored without `--behind-proxy` lets a Host-rewriting proxy in front of a credential-less loopback console in (security, 0/2) | Refuted: that is today's loopback-trust rule and no plan path fronts a credential-less console (the launchd service requires `--auth-file`). No change. |
| R2 | The acceptance-checklist probe cannot detect the one case `--behind-proxy` exists for (operability, 1/1 cast, under-verified) | Same as Codex's still-open `hosted-success-is-not-gated`; resolved below. |

### Round 3 — Discuss (Visionary responses, on the record)

| # | Finding | Codex | Visionary response |
| --- | --- | --- | --- |
| 18 | hosted-success-is-not-gated | FIX | **Agree.** Rather than forcing a 403, `/api/health` now always reports `received_host` and `forwarded_host` (values the client itself sent; nothing sensitive), so the probe is diagnostic on 200 and 403 alike. This is the one existing-test edit of the cycle: the equality assertion at `tests/test_dca_console.py:88` becomes a two-key check. Forwarded-host precedence is now specified: under `--behind-proxy`, a present `X-Forwarded-Host` must be a configured name and becomes the effective authority (Host must still be in `hosts(port)`); present-but-unconfigured → 403; absent → Host rules. Test: loopback `Host` + `X-Forwarded-Host: mac.tailnet.ts.net` + `Origin: https://mac.tailnet.ts.net` → 200 under `--behind-proxy`, 403 without it. Checklist rule: add `--behind-proxy` when the public name appears under `forwarded_host` and not `received_host`. |
| 22 | fifo-auth-file-blocks-before-validation (new) | FIX | **Agree.** `O_NONBLOCK` added to the open flags (no effect on regular-file reads; a FIFO open returns at once and `S_ISREG` rejects it). FIFO regression: `os.mkfifo`, no writer, `load_auth_file` must raise within a 5-second thread join. |
| 23 | rolling-default-breaks-demo-pipeline (new) | FIX | **Agree; my error in revision 4.** `_run` keeps its current selection (`settings.day` in demo mode, `default_demo_day()` in local mode); `default_day()` feeds only `/api/bootstrap`, `snapshot()` and `default_date`. Regression: unpinned local `Settings` with the clock on a Saturday → `demo-dca` still targets the previous full session. |

Solve: PLAN.md revised to revision 5 (see "Changes since round 3" at its end).

## Round 4 — revised plan submitted

Timestamp: 2026-09-13T07:25:21Z
PLAN.md sha256: 3ecb5f9372047f19…  (VTO.md, DEFERRED.md unchanged since round 2)

### Prompt sent (Round 4)

> Round 4. PLAN.md is revised to "Revision 5" addressing the three items you left open in
> round 3 plus two findings from an independent review; VTO.md and DEFERRED.md are unchanged.
> Re-read PLAN.md end to end; "Changes since round 3" at the end is the index. You remain
> read-only.
>
> Resolutions:
>
> - hosted-success-is-not-gated — `/api/health` now always reports `received_host` and
>   `forwarded_host` (what the client sent), so the probe is diagnostic on 200 and 403 alike;
>   this is the cycle's one existing-test edit (the equality at tests/test_dca_console.py:88
>   becomes a two-key check). Forwarded-host precedence is specified: under `--behind-proxy`
>   a present `X-Forwarded-Host` must be a configured name and becomes the effective
>   authority while `Host` must still be in `hosts(port)`; present-but-unconfigured → 403;
>   absent → plain Host rules. New test: loopback Host + `X-Forwarded-Host: mac.tailnet.ts.net`
>   - `Origin: https://mac.tailnet.ts.net` → 200 with `--behind-proxy`, 403 without. Both
>   checklists read the body on 200 or 403 and add `--behind-proxy` only when the public name
>   is under `forwarded_host` and not `received_host`.
> - fifo-auth-file-blocks-before-validation — `O_NONBLOCK` added to the open flags; the FIFO
>   regression uses `os.mkfifo` with no writer and must raise within a 5-second thread join;
>   the descriptor test asserts `O_NOFOLLOW` and `O_NONBLOCK` are among the recorded flags.
> - rolling-default-breaks-demo-pipeline — `_run` keeps its existing selection
>   (`settings.day` in demo mode, `default_demo_day()` in local mode, dca_console.py:252);
>   `default_day()` feeds only bootstrap, snapshot default and default_date; regression:
>   unpinned local Settings with the clock on a Saturday → `demo-dca` still targets the
>   previous full session.
> - (independent review) The auth-failure limiter is a per-server `AuthFailureLog(interval,
>   clock, stream)` object injectable through `make_server(..., failure_log=None)`, never
>   module-global; per-line tests use `interval=0` or a fake clock, burst/thread tests a fresh
>   instance with the real interval.
> - (independent review) The renderer's default interpreter is a module-level
>   `DEFAULT_PYTHON` read at call time; the missing-default test monkeypatches it to a
>   missing path and a positive counterpart patches it to `sys.executable`, so both pass on
>   the Mac (where `.venv` exists) and in CI (where it does not).
>
> For each of the three round-3 open items, confirm resolved or still open using the same
> [SEVERITY] short-title label. New findings are allowed but should be rare this late — flag
> anything major. End with the same single verdict line as before, as the literal last line:
> `VERDICT: SAME PAGE` if you have zero open FIX or KILL items, otherwise
> `VERDICT: BLOCKED — N open finding(s)`.

### PLAN.md revision 5 — sections changed since round 3

````markdown
## Design decisions

| Topic | Decision | Why |
|---|---|---|
| Secret source | `--auth-file PATH`: one line `user:password`; POSIX only (`os.name == "posix"`, otherwise `ValueError` — fail closed, no Windows tolerance for this feature); opened with `os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)` (a symlink fails with `ELOOP`; `O_NONBLOCK` makes a FIFO open return at once instead of waiting for a writer, and has no effect on a regular-file read), validated with `os.fstat` **on that descriptor** (`S_ISREG`; `st_uid == os.getuid()`; `st_mode & 0o077 == 0`; `st_size ≤ 4096`) and read through the same descriptor with a bounded `os.read`, so the bytes provably come from the checked inode; username printable ASCII without `:`; password ≥ 12 chars. Errors name the rule, never the contents | An env var would be copied into every job child by `env = dict(os.environ)` (`dca_console.py:237`), show in `launchctl print` / `ps -E`, and sit inside the plist. A path in argv leaks nothing. Ownership and regular-file checks are what make "0600" mean something, and checking a path then reading it separately is a check/use race a writable parent directory could exploit (Same Page round 2). Without `O_NONBLOCK` a FIFO at `--auth-file` would hang the console and the renderer before `fstat` could reject it, instead of exiting 2 (Same Page round 3) |
| Fail-closed construction | All access rules are enforced in `ConsoleAccess.__post_init__`, so an invalid combination cannot be constructed anywhere — not in `main()`, not by a future caller of `make_server()`. `main()` maps `ValueError` → `parser.error` (exit 2) | A CLI-only check would let another entry point open a reachable socket without a credential |
| Auth scheme | HTTP Basic on every route except `/api/health`, only when a credential is configured; `secrets.compare_digest` on `b"user:password"`; failure → `401` + `WWW-Authenticate: Basic realm="DCA console", charset="UTF-8"` + JSON error | Native browser prompt on a phone; zero frontend change for auth because same-origin `fetch` (`console/app.js:381,384,411`) and the `<a download>` export (`console/index.html:38`) attach credentials automatically |
| `/api/health` unauthenticated and diagnostic | Still behind the Host/Origin check; body is `{"status","mode","received_host","forwarded_host"}` — the two host values are what the client itself sent (≤ 253 chars each, or null) | Lets probes and the launchd runbook work without embedding the password anywhere, and makes the runbook probe diagnostic on a 200 as well as on a 403: a front that rewrites `Host` to the loopback authority and carries the public name only in `X-Forwarded-Host` would otherwise return a bare 200 and the checklist could not tell that `--behind-proxy` is needed (Same Page round 3). This is the cycle's one edit to an existing test: the exact-equality assertion at `tests/test_dca_console.py:88` becomes a check of `status` and `mode` |
| `X-Console-Token` | Keep, plus a frontend re-bootstrap: on a 403 from `POST /api/jobs` the page clears `state.token`, fetches `/api/bootstrap` once and retries once | Basic credentials are ambient like cookies; the token (fetched same-origin, no CORS headers) is the CSRF defence. A launchd restart rotates it; `do_POST` checks the token before `submit()` (`dca_console.py:390`), so a 403 proves the job was never accepted and one retry is safe |
| Rolling default day | `Settings.day` becomes `date \| None`. `None` means *unpinned* (local mode started without `--date`): `ConsoleApp.default_day()` returns `datetime.now(ET).date()` at request time. An explicit `--date`, or a fixture-provided day, *pins* it: `default_day()` returns `settings.day`. Demo mode always has a concrete day (`default_demo_day()`). `default_day()` feeds `/api/bootstrap`, the `snapshot()` default and `default_date` **only**; `_run` keeps its existing day selection (`settings.day` in demo mode, `default_demo_day()` in local mode, `dca_console.py:252`), because the demo pipeline needs a full market session, not a calendar day | `main()` freezes `Settings.day` at startup (442); a service that survives midnight would otherwise open new tabs on stale data. `--local --date …` and the existing pinned-day tests must keep their meaning (Same Page round 2) |
| `--bind ADDR` | IPv4 literal only (`ipaddress.IPv4Address`), default `127.0.0.1`; foreground use only — the launchd renderer has no `--bind`. A non-loopback bind makes the startup banner (pure `startup_banner(access, port)`) print `plain HTTP: the Basic credential and console token are visible to anyone on this network` | `HTTPServer` is AF_INET; hostnames/IPv6 deferred; the persistent service stays loopback-only; the operator must see the plaintext caveat where they start the process |
| `--trusted-host HOST[:PORT]` (repeatable) | lowercased; hostname regex; no scheme, path, wildcard or whitespace; port ≤ 65535 | Extra names accepted in `Host`. Also accept `https://` origins (today only `http://`, `dca_console.py:344`, which would block POSTs behind an HTTPS proxy) |
| `hosts(port)` | `{127.0.0.1:port, localhost:port}` ∪ `{bind:port}` when bind is a specific non-loopback IP ∪ `trusted_hosts`; `0.0.0.0` adds nothing | Phase A needs no `--trusted-host`; with `0.0.0.0` the server cannot know which address the client typed |
| Effective authority and `Origin` | The *effective authority* is the accepted `Host`, or under `--behind-proxy` the accepted `X-Forwarded-Host`. When `Origin` is present it must equal exactly `http://<authority>` or `https://<authority>` | Membership of both in allowed sets is not enough: a loopback `Host` with a tailnet `Origin` (or vice versa) must be refused |
| `--behind-proxy` | Precedence: when `X-Forwarded-Host` is present it must be a **configured** name (never a loopback name) and becomes the effective authority, while `Host` must still be in `hosts(port)`; present-but-unconfigured → 403; absent → the plain `Host` rules apply. Requires ≥1 `--trusted-host` **and** a loopback bind | A LAN-reachable socket that honours forwarded headers would let any LAN client forge them; the precedence rule is what lets a Host-rewriting front work with `Origin` bound to the public name (Same Page round 3) |
| Fail-closed matrix (in `__post_init__`) | non-loopback bind without a credential; any `trusted_hosts` without a credential; `behind_proxy` without `trusted_hosts` or with a non-loopback bind; `0.0.0.0` without `trusted_hosts`; malformed bind / host values | Nothing listens on a reachable address without a credential |
| `Sec-Fetch-Site: cross-site` | Rejected everywhere **except** `GET /` with `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. Every `/api/*` route, including GETs, stays cross-site-rejected | Cloudflare Access login is a redirect chain through `<team>.cloudflareaccess.com`; Fetch Metadata computes the site over the whole chain, so the first document load after login arrives `cross-site`. The document route is side-effect-free; `/api/snapshot` and `/api/export` are not (they call `_save_summary()`), so they keep the strict rule. Existing test at `tests/test_dca_console.py:122-124` still gets 403 |
| 403 body | adds `received_host` (≤ 253 chars) and `forwarded_host` | The runbook's "what does the backend see" probe; JSON with `nosniff`, no HTML |
| Brute-force backoff | Deferred (see `DEFERRED.md`); 12-char minimum, documented generator | Behind either proxy every request is from `127.0.0.1`; per-IP state becomes a global lockout lever |
| Auth-failure log line | One fixed-shape stderr line per failure, built exception-safely: the route comes from `urlsplit(self.path).path` inside `try/except` (a malformed target logs the fixed text `malformed-target`); everything outside printable ASCII is stripped first; then `redact()`; **then** truncation to 128 characters; prefixed with client address and method. The limiter is a small object, `AuthFailureLog(interval=5.0, clock=time.monotonic, stream=sys.stderr)`, created per `make_server` call (injectable as `make_server(app, port, access=None, failure_log=None)`), never module-global; it is lock-protected, emits at most one line per interval and reports the suppressed count on the next emitted line | Any unauthenticated LAN/tailnet client can hit 401/403 at line rate; the line must not let it grow `launchd.err.log` unboundedly, inject newlines, or plant a credential-shaped string. Truncating before redaction could leave a fragment the regex no longer recognises; the server is threaded, so limiter state needs a lock (Same Page round 2); a process-global limiter would make every 401/403 test order-dependent, so tests inject `interval=0` or a fake clock (panel, round 3) |
| SIGTERM | Context manager installs a handler that raises `KeyboardInterrupt`, reusing the Ctrl+C teardown (`dca_console.py:492-497`); restores the previous handler; entered **before** the readiness banner is printed and covering server creation through `serve_forever` (the serving branch only, not `--once`/`--run`/`--check`) | `server.shutdown()` from the handler would deadlock (same thread as `serve_forever`). A handler installed after the banner would let a SIGTERM sent on seeing the banner hit the default action and exit `-15` (Same Page round 2). Worst-case teardown ≈ 11 s (`dca_console_jobs.py:210-226`), inside the plist's `ExitTimeOut = 30` |
| Handler hygiene | `Handler.timeout = HANDLER_TIMEOUT_SECONDS` (30; today `None`, only POST bodies get `settimeout(5)` at line 400); `version_string()` override | An idle LAN/proxy connection must not pin a thread; `Server:` should not advertise the Python version |
| Long uptime | Cap `ConsoleApp.signatures` to the last 32 days | Everything else is already bounded |
| launchd | `deploy/launchd/render_console_agent.py` renders the plist with `plistlib` (`--print` / `--install`), validates the auth file via `load_auth_file`, takes `--python PATH` (default `<checkout>/.venv/bin/python`; refuses when the chosen interpreter does not exist), resolves **one** artifact root with precedence `--cron-tracking-root` > `$DCA_CRON_TRACKING_ROOT` > `dca_paths` default, always writes it into the plist environment and derives the log paths from that same value, creates the log directory, prints the `launchctl` commands including the apply pair (`bootout … 2>/dev/null || true`, then `bootstrap`). Fixed label `com.all-stock-dca.console`. Optional `--octopus-run DIR` passthrough. No `--bind`. No placeholder template | Same "logic in a script, not the unit" rationale as `deploy/run-drift-check.sh:6-17`; `plistlib` cannot mis-escape; one resolved root means logs and console artifacts can never land in different trees; a fixed label removes an unvalidated path/shell fragment |
| pyproject | No change | Console is checkout-only; `deploy/` is not shipped |
| Proxy `Host` semantics (read from source at HEAD, not a live install — uncertainty acknowledged) | Tailscale Serve preserves `Host` and adds `X-Forwarded-Host/-Proto/-For` + `Tailscale-User-*`; cloudflared preserves `Host` unless `httpHostHeader` is set | Accept the configured name in `Host` or (with `--behind-proxy`) `X-Forwarded-Host`; every runbook starts with a `curl -si …/api/health` whose 403 body prints what arrived |
| Credentials in proofs | `curl -K <(printf 'user = "%s"\n' "$(<"$AUTH")")` | The curl config arrives through a `/dev/fd` path; argv never holds the secret |

### Rock 1 — Basic auth from a 0600 file (loopback-safe, no exposure yet)

**Scope.** Frozen `ConsoleAccess` dataclass beside `Settings` (`username: str = ""`,
`credential: bytes | None = field(default=None, repr=False)`) with `__post_init__`
validation of the credential shape; `load_auth_file(path) -> tuple[str, bytes]` with the
rules above (POSIX only, `O_NOFOLLOW | O_NONBLOCK` open, `fstat` on the descriptor,
regular file, owner, mode, size ≤ 4096, bounded read through that same descriptor, one
line, `:`), error messages naming the rule but never the contents; `ConsoleAccess.authorized(headers)` (`Basic <b64>`,
scheme case-insensitive, `base64.b64decode(..., validate=True)`, `compare_digest`);
`--auth-file`; 401 path on every route except `/api/health` — order `trusted` → 403,
`authorized` → 401, then token → 403 on POST; `dca_console_jobs.register_secret()`
consulted by `redact()`; the exception-safe, control-stripped, redact-then-truncate
auth-failure line emitted through a per-server `AuthFailureLog` (design table). `make_server(app, port, access=None)`
keeps today's behaviour by default so all 78 existing tests pass untouched. Frontend untouched.

**Non-goals.** `--bind`, trusted hosts, proxy headers (Rock 2); backoff; UI hint that
auth is on.

**Files.** `dca_console.py`, `dca_console_jobs.py`, `tests/test_dca_console.py`.

**Tests.** `test_auth_file_rules_fail_closed` (missing, mode 0644, symlink to a good file
(refused by `O_NOFOLLOW`), a FIFO made with `os.mkfifo` and no writer (must raise within a
5-second thread join, so a regression to a blocking open fails instead of hanging CI),
directory, a file larger than 4096 bytes, wrong owner via
monkeypatched `os.getuid`, non-POSIX via monkeypatched `os.name`, no colon, empty, two
lines, 11-char password → `ValueError` whose message lacks the file contents; good file →
`("dca", b"dca:s3cret-pw-abcdefg")`); `test_auth_file_is_read_from_the_checked_descriptor`
(monkeypatched `os.open`/`os.fstat`/`os.read` record the descriptor and flags: `fstat` and
`read` receive the fd `open` returned, `O_NOFOLLOW` and `O_NONBLOCK` are among the recorded
flags, and no path-based `open()`/`Path.read_*` happens);
`test_basic_auth_required_on_every_route_except_health` (every asset, `/api/bootstrap`,
`/api/snapshot`, `/api/export`, `/api/docs` → 401 with `WWW-Authenticate` starting
`Basic realm=`; `/api/health` → 200; POST with right token but no credentials → 401 and
`app.submit` never called); `test_basic_auth_wrong_credentials_are_rejected` (wrong
password, wrong user, undecodable base64, `Bearer` scheme → 401);
`test_basic_auth_correct_credentials_pass_and_token_still_required` (bootstrap 200; POST
with credentials but no token → 403; with both → 202);
`test_password_never_appears_in_responses_or_redaction` (bodies/headers lack it; `redact()`
masks it; `repr(access)` lacks it); `test_auth_failure_is_logged_without_credentials`;
`test_auth_failure_log_line_is_bounded_and_redacted` (a 60 KB request path and a path
containing the fixture password each yield one short line with the route only and the
password masked; a password straddling the 128-character cutoff is still masked; a path
carrying `\r\n` and other control characters yields exactly one line; a malformed request
target yields the fixed `malformed-target` text and no traceback; a burst of 50 failures
yields at most two lines plus a suppressed count). The per-line sub-cases build their
server with `AuthFailureLog(interval=0, stream=io.StringIO())` (or advance an injected fake
clock) and assert on that stream; the burst case uses a fresh log with the real interval;
no sub-case depends on wall-clock spacing. `test_auth_failure_limiter_is_thread_safe`
(20 threads failing concurrently against one fresh `AuthFailureLog` → at most two lines
and a consistent suppressed count).

**Proof.**
```bash
CONSOLE_TESTS=(tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py); AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest "${CONSOLE_TESTS[@]}" tests/test_config_invariants.py -q \

.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done
curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/health
curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/bootstrap
curl -s -o /dev/null -w '%{http_code}\n' -K <(printf 'user = "%s"\n' "$(<"$AUTH")") http://127.0.0.1:8765/api/bootstrap
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
```
Expected: ruff clean; 87 console passes (78 + 9) and the invariants file green; curl
prints `200 401 200`; then `Stopping local console.` and `exit=0`.

### Rock 2 — `--bind`, `--trusted-host`, `--behind-proxy`, hardened handler (Phase A reachable)

**Scope.** Extend `ConsoleAccess` with `bind`, `trusted_hosts`, `behind_proxy` and move
the whole fail-closed matrix into `__post_init__`; pure `hosts(port)`,
`effective_authority(headers, port) -> str | None` and `trusted(headers, port)`; replace
`_trusted()` (`dca_console.py:339-345`) with the forwarded-host precedence rule; `Origin`
bound to the effective authority with `http://` or `https://`; `/api/health` body gains
`received_host` / `forwarded_host` (the one existing-test edit: the equality at
`tests/test_dca_console.py:88` becomes a two-key check); cross-site tolerance for `GET /` navigations only; 403 diagnostic
body; `ThreadingHTTPServer((access.bind, port), …)` (line 412); `HANDLER_TIMEOUT_SECONDS
= 30`; `version_string()`; pure `startup_banner(access, port)` replaces the banner at 472-473 and prints bind,
port, trusted hosts, `auth: basic user=<name>` (never the password) and, for a
non-loopback bind, the plaintext warning line; reword the `--port` help (426) and the
bind-failure message (470); `webbrowser.open` targets the bind IP when it is a specific
address.

**Non-goals.** IPv6 / hostname binds; in-process TLS; proxy identity headers; UI changes.

**Files.** `dca_console.py`, `tests/test_dca_console.py`.

**Tests.** `test_console_access_construction_fails_closed` (`ConsoleAccess(...)` raises
`ValueError` for the whole matrix plus `bind="not-an-ip"`, `"::1"`, trusted-host values
`http://x`, `x/y`, `x:99999`, `x y`, `*.ts.net`);
`test_invalid_access_never_constructs_a_server` (`ThreadingHTTPServer` monkeypatched to
`pytest.fail`; `make_server(app, 0, ConsoleAccess(bind="192.0.2.10"))` raises first);
`test_exposure_flags_fail_closed_from_cli` (`main([...])` → `SystemExit` code 2 for
`--bind 192.0.2.10` without `--auth-file`, `--trusted-host x` without `--auth-file`,
`--behind-proxy` without `--trusted-host`, `--behind-proxy --bind 192.0.2.10 …`,
`--bind 0.0.0.0 --auth-file …` without `--trusted-host`);
`test_trusted_hosts_accept_configured_names_and_https_origin` (`Host: mac.tailnet.ts.net`
+ `Origin: https://mac.tailnet.ts.net` + credentials → 200; upper-case Host → 200;
`Host: other.ts.net` → 403 with `received_host == "other.ts.net"`; loopback Host still 200);
`test_origin_must_match_effective_authority` (loopback `Host` + `Origin:
https://mac.tailnet.ts.net` → 403; tailnet `Host` + `Origin: http://127.0.0.1:<port>` → 403;
under `behind_proxy`, `X-Forwarded-Host: mac.tailnet.ts.net` + `Origin:
https://mac.tailnet.ts.net` → 200);
`test_forwarded_host_is_ignored_unless_behind_proxy` (`Host: untrusted.example` +
`X-Forwarded-Host: mac.tailnet.ts.net` → 403 on a non-proxy server and also 403 with
`behind_proxy=True` because `Host` must still be in `hosts(port)`; `X-Forwarded-Host:
127.0.0.1:<port>` on the proxy server → 403; `X-Forwarded-Host: other.ts.net` (present but
unconfigured) → 403); `test_forwarded_host_takes_precedence_as_effective_authority`
(loopback `Host` + `X-Forwarded-Host: mac.tailnet.ts.net` + `Origin:
https://mac.tailnet.ts.net` → 200 with `behind_proxy=True`, 403 without it);
`test_health_reports_received_and_forwarded_host` (200 body carries the `Host` the client
sent and `null` / the forwarded value; the existing `status`/`mode` keys unchanged);
`test_hosts_for_bind_addresses` (pure);
`test_non_loopback_bind_banner_warns_about_plaintext` (pure `startup_banner`: the line
appears for `bind="192.0.2.10"`, not for loopback, and never contains the password);
`test_cross_site_navigation_allowed_only_for_document_route` (GET `/` with
`Sec-Fetch-Site: cross-site, Sec-Fetch-Mode: navigate, Sec-Fetch-Dest: document` → 200;
the same headers on `/api/bootstrap`, `/api/snapshot` and `/api/export` → 403, no summary
file is written and a monkeypatched `app.snapshot` is never invoked; `Sec-Fetch-Mode:
cors` on `/` → 403; POST → 403);
`test_idle_connection_is_closed_by_handler_timeout` (`HANDLER_TIMEOUT_SECONDS = 0.2`, raw
socket that sends nothing gets EOF within 2 s); `test_server_header_does_not_leak_python_version`.

**Proof.**
```bash
CONSOLE_TESTS=(tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py); AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest "${CONSOLE_TESTS[@]}" tests/test_config_invariants.py -q \

LAN=$(ipconfig getifaddr en0)
.venv/bin/python from.py --local --no-browser --bind "$LAN" --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null "http://$LAN:8765/api/health" && break; sleep 0.25; done
curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/health"
curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/snapshot"
curl -s -o /dev/null -w '%{http_code} ' -K <(printf 'user = "%s"\n' "$(<"$AUTH")") "http://$LAN:8765/api/snapshot"
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' "http://$LAN:8765/api/health"
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
.venv/bin/python from.py --local --no-browser --bind 0.0.0.0; echo "exit=$?"
```
Expected: ruff clean; 100 console passes (87 + 13); the banner shows the plaintext
warning; curl prints `200 401 200 403`; `Stopping local console.` then `exit=0`; the last
command prints the fail-closed message and `exit=2`. Then a phone on the same wifi:
`http://<LAN>:8765` prompts once, shows `LOCAL` mode with real runs, and "Export JSON"
downloads. (Plaintext on the home wifi; disposable credential.)

### Rock 3 — Clean SIGTERM, restart resilience, long-uptime bounds

**Scope.** `_sigterm_as_keyboard_interrupt()` context manager entered before the
readiness banner (472-473) and covering server creation through `serve_forever` (491),
restoring the previous handler, serving branch only; `Settings.day: date | None` with
`main()` passing `args.date` (None when absent in local mode) and `ConsoleApp.default_day()`
(unpinned → today in ET at request time; pinned → `settings.day`) used by `/api/bootstrap`,
`snapshot()` and `default_date` only — `_run` keeps `settings.day` (demo) /
`default_demo_day()` (local) exactly as at `dca_console.py:252`; cap `signatures` (129,
225) to the last 32 days; `console/app.js`:
on a 403 from `POST /api/jobs`, clear `state.token`, re-bootstrap once, retry once, then
surface the error.

**Non-goals.** SIGHUP reload; job-file pruning; log rotation.

**Files.** `dca_console.py`, `console/app.js`, `tests/test_dca_console.py`.

**Tests.** `test_sigterm_stops_console_cleanly` (posix-only subprocess: spawn
`from.py --local --no-browser --port 0 --root <tmp> --output-dir <tmp>`, wait for the
`DCA console (local)` banner, `send_signal(SIGTERM)`, `wait(timeout=20)` → returncode 0
and stdout contains `Stopping local console.`);
`test_sigterm_handler_raises_and_restores_previous_handler`;
`test_unpinned_local_default_day_rolls_over_midnight` (`Settings(day=None)`, monkeypatched
clock: bootstrap `default_date` and `snapshot()["daily"]["date"]` change from one ET day to
the next without a restart); `test_explicit_date_stays_pinned` (local mode with a concrete
`day`, and demo mode, keep `default_date` and the snapshot default on that day across the
same clock change; `main(["--local", "--date", …])` produces a pinned `Settings`, and
without `--date` an unpinned one); `test_demo_pipeline_day_is_not_the_calendar_day`
(unpinned local `Settings` with the clock on a Saturday: `_run("demo-dca")` passes the
previous full session's date to `--now`, captured from a monkeypatched `_command`);
`test_summary_signature_cache_is_bounded` (100 distinct days → `len(app.signatures) <= 32`).

**Proof.**
```bash
CONSOLE_TESTS=(tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py); AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest "${CONSOLE_TESTS[@]}" -q \

.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & pid=$!
for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done
kill -TERM "$pid"; wait "$pid"; echo "exit=$?"
! lsof -nP -iTCP:8765 -sTCP:LISTEN && echo "port released"
```
Expected: 106 passes; `Stopping local console.` then `exit=0` within seconds, then
`port released`. Manual (after Rock 4): with a tab open, `launchctl kickstart -k
gui/$(id -u)/com.all-stock-dca.console`, then click "Test DCA pipeline" in the same tab
without reloading → the job starts.

### Rock 4 — launchd agent renderer

**Scope.** `deploy/launchd/render_console_agent.py` (stdlib `argparse`, `plistlib`, `os`,
`pathlib`; `sys.path.insert(1, <checkout>)` per the ticker-advisor precedent, then
`from dca_paths import cron_tracking_root` and `from dca_console import load_auth_file`).
Args: `--auth-file` (required), `--python PATH` (argparse default `None`; `main()` falls
back to the module-level `DEFAULT_PYTHON = CHECKOUT / ".venv" / "bin" / "python"` read at
call time so tests can monkeypatch it; the chosen interpreter must exist), `--port 8765`, `--trusted-host` (repeatable),
`--behind-proxy`, `--octopus-run DIR` (optional, must be an existing directory),
`--cron-tracking-root DIR` (optional), exactly one of `--print` / `--install`. No `--bind`,
no `--label`. The renderer resolves `ROOT` once with precedence `--cron-tracking-root` >
`$DCA_CRON_TRACKING_ROOT` > `dca_paths.cron_tracking_root()` default, and uses it for both
the plist environment and the log paths, so the plist is self-consistent whatever the
invoking shell exported. Rendered dict:

```
Label                = com.all-stock-dca.console
ProgramArguments     = [<python>, <checkout>/from.py, --local, --no-browser,
                        --port, 8765, --auth-file, <abs path>, (--trusted-host, H)*, (--behind-proxy),
                        (--octopus-run, <abs dir>)]
WorkingDirectory     = <checkout>
RunAtLoad            = true
KeepAlive            = { SuccessfulExit = false }   # restart on failure; a clean SIGTERM exit (main returns 0) is not restarted
ThrottleInterval     = 15                            # a persistent bind failure (main returns 1) retries every 15 s, no hot loop
ExitTimeOut          = 30
ProcessType          = Background
StandardOutPath      = <ROOT>/console/launchd.out.log
StandardErrorPath    = <ROOT>/console/launchd.err.log
EnvironmentVariables = { PYTHONUNBUFFERED = "1", PATH = "/usr/bin:/bin:/usr/sbin:/sbin", DCA_CRON_TRACKING_ROOT = <ROOT> }
```

No secret anywhere in the plist. `--install` writes
`~/Library/LaunchAgents/com.all-stock-dca.console.plist` (0644), creates
`<ROOT>/console/` (launchd creates log files but not their parent), and prints:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist   # load + start
launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state|pid'             # status
launchctl kickstart -k gui/$(id -u)/com.all-stock-dca.console                            # restart an UNCHANGED definition (e.g. after rotating the auth file)
launchctl bootout gui/$(id -u)/com.all-stock-dca.console                                 # stop + unload; REQUIRED before bootstrap whenever the plist changed
# apply pair, printed last, for a changed plist:
launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist
```

`.gitignore` gains `deploy/launchd/*.plist` beside the existing `deploy/*.env` line.

**Non-goals.** Running `launchctl` from the script; a `/Library/LaunchDaemons` variant;
a LAN-bound (Phase A) service; installing Tailscale or cloudflared.

**Files.** `deploy/launchd/render_console_agent.py` (new), `tests/test_console_launchd.py`
(new; imports the script via `importlib.util.spec_from_file_location` so coverage counts
it), `.gitignore`.

**Tests.** Every test passes `--python sys.executable` (CI has no `.venv`; no test may
assume `<checkout>/.venv` exists). `test_renders_expected_program_arguments_and_paths`
(loopback-only Phase B form with and without `--trusted-host mac.tailnet.ts.net
--behind-proxy` and `--octopus-run`; `plistlib.loads` output has the exact
`ProgramArguments` with `[0] == sys.executable`,
`Label == "com.all-stock-dca.console"`, `KeepAlive == {"SuccessfulExit": False}`,
`RunAtLoad`, `ExitTimeOut == 30`, log paths under `<ROOT>/console`, no
`EnvironmentVariables` key containing `PASSWORD`);
`test_custom_root_is_used_for_both_environment_and_logs` (`--cron-tracking-root <tmp>` →
`EnvironmentVariables["DCA_CRON_TRACKING_ROOT"] == <tmp>` and both log paths start with
`<tmp>/console/`); `test_root_from_environment_is_used_when_flag_absent` (monkeypatched
`DCA_CRON_TRACKING_ROOT`, no flag → the same root in the env entry and both log paths);
`test_rendered_plist_contains_no_secret`; `test_renderer_fails_closed` (loose-mode auth
file, `--behind-proxy` without `--trusted-host`, `--octopus-run` that is not a directory,
`--python` pointing at a missing file, and — with `DEFAULT_PYTHON` monkeypatched to
`tmp_path / "missing"` — no `--python` given → exit 2; no `--bind` / `--label` arguments
exist); `test_default_python_is_used_when_flag_absent` (`DEFAULT_PYTHON` monkeypatched to
`sys.executable`, no `--python` → `ProgramArguments[0] == sys.executable`);
`test_install_writes_agent_and_creates_log_dir` (`HOME` → tmp; 0644; log dir exists);
`test_install_prints_bootout_before_bootstrap` (the printed apply sequence has
`launchctl bootout` before `launchctl bootstrap`).

**Proof.**
```bash
CONSOLE_TESTS=(tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py); AUTH=~/.config/all-stock-dca/console-auth
git add -N deploy/launchd tests/test_console_launchd.py \
&& .venv/bin/python -m ruff check . && .venv/bin/python -m pytest "${CONSOLE_TESTS[@]}" tests/test_console_launchd.py tests/test_config_invariants.py -q \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --print | plutil -lint - \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --install \
&& { launchctl bootout gui/$(id -u)/com.all-stock-dca.console 2>/dev/null || true; } \
&& launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist \
&& for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8765/api/health && break; sleep 0.25; done \
&& curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/health \
&& launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state = |pid = '
```
Expected: tests green (106 console + 8 launchd); `plutil` reports OK and exits 0; `200`; `state = running`
with a pid. Afterwards `launchctl bootout gui/$(id -u)/com.all-stock-dca.console` and
`tail -2 cron_tracking/console/launchd.out.log` shows `Stopping local console.`

## Operator acceptance checklist (post-merge; the cycle is *deployed* only when one front passes)

Common first step: rotate the credential file (regenerate it with the one-liner above; the
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

## Changes since round 3 (Same Page Meeting + second panel)

- fifo-auth-file-blocks-before-validation → `O_NONBLOCK` in the open flags; FIFO
  regression with a bounded-time assertion; descriptor test checks the flags (Rock 1).
- rolling-default-breaks-demo-pipeline → `_run` keeps its existing day selection;
  `default_day()` feeds only bootstrap/snapshot/default_date; Saturday regression (Rock 3).
- hosted-success-is-not-gated → `/api/health` always reports `received_host` /
  `forwarded_host` (one existing-test edit); forwarded-host precedence specified and
  tested; checklists read the body on 200 or 403 (Rocks 2, 5).
- Process-global limiter → per-server `AuthFailureLog` object, injectable; tests use
  `interval=0` / fake clock (Rock 1).
- Renderer default interpreter → module-level `DEFAULT_PYTHON` read at call time;
  missing-default and positive-default tests monkeypatch it (Rock 4).

````

### Round 4 — Codex reply

Timestamp: 2026-09-13T07:30:11Z

> I’ll re-audit Revision 5 against the current implementation and tests, concentrating on the three reopened boundaries and whether the two independent-review additions introduce portability or CI gaps.
> The runtime fixes themselves are coherent. One test-harness detail remains under scrutiny: a timed join only prevents a FIFO regression from hanging CI if the still-blocked worker is daemonized or explicitly released during cleanup.
> [MAJOR] hosted-success-is-not-gated — RESOLVED: health diagnostics now cover both statuses, forwarded-host precedence is fail-closed and Origin-bound, the loopback-host fallback is tested, and both checklists require successful reprobe and phone acceptance.
>
> [MAJOR] fifo-auth-file-blocks-before-validation — RESOLVED: `O_NONBLOCK` lets a writerless FIFO reach same-descriptor `fstat` rejection immediately, with flag and bounded-completion regressions planned.
>
> [MAJOR] rolling-default-breaks-demo-pipeline — RESOLVED: `_run` retains its recent-full-session selection independently of the rolling hosted date, with a Saturday regression checking `--now`.
>
> The per-server `AuthFailureLog` and call-time `DEFAULT_PYTHON` designs introduce no new major issue.
>
> Verdicts:
>
> - hosted-success-is-not-gated — ACCEPT: the complete detection-to-acceptance path is now specified and tested.
> - fifo-auth-file-blocks-before-validation — ACCEPT: the runtime hang is closed.
> - rolling-default-breaks-demo-pipeline — ACCEPT: existing demo behavior is preserved.
>
> VERDICT: SAME PAGE

### Round 4 — Discuss and close

All three round-3 items ACCEPTed; the two panel-driven additions (per-server
`AuthFailureLog`, call-time `DEFAULT_PYTHON`) raised no new finding. Codex's one aside —
that a timed join only protects CI if the still-blocked FIFO worker is a daemon thread or
released in cleanup — is adopted as an implementation note in the Rock 1 contract
(`threading.Thread(daemon=True)` for the bounded-time assertion).

**Same Page reached at round 4 of 5.** PLAN.md revision 5 is the agreed build plan; the
Rocks proceed in the order 1 → 2 → 3, 4 after 1, 5 last, each built by Codex in
`--sandbox workspace-write` and reviewed/proved by the Visionary.

editorial: removed trailing backslash continuations before blank lines in Rock 1-3 proofs (2026-09-13T07:31:03Z); no semantic change
editorial (2026-09-13T08:28:30Z): Rock 1 and 2 proof expectations corrected — SIGTERM is unhandled until Rock 3, so their stop step exits 143, not 0 (flagged by the Integrator in the Rock 1 report)

## Rock 1 — Visionary review

Timestamp: 2026-09-13T08:47:04Z. Codex built Rock 1 (`dca_console.py`, `dca_console_jobs.py`,
`tests/test_dca_console.py`, nine new tests). Its sandbox could not bind loopback ports, so
13 HTTP-backed tests and the curl proof did not run there. The Visionary read the full diff
(764 lines) and ran the proof: ruff clean, 292 passed (87 console + 205 invariants), curl
`200 401 200`, stop step exit 143 (SIGTERM is Rock 3; plan expectation corrected). An
adversarial diff review (three lenses, three skeptics per finding) confirmed 3 of 8 findings:

| # | Finding | Response |
| --- | --- | --- |
| 1 | [MAJOR] Non-ASCII password leaks its printable projection into the auth-failure log — `http.server` decodes the request line as ISO-8859-1, so the UTF-8 `str` registration never matches the wire spelling (reproduced independently by the Visionary: `route=/x/psswrd-abcdefg`) | **FIX round 1.** Register the ISO-8859-1 and percent-encoded spellings; redact the unquoted target too; raw-socket regression test. |
| 2 | [MINOR] No "why" comment on the `O_NOFOLLOW \| O_NONBLOCK` flags line | **FIX round 1.** House-voice comment citing rounds 2 and 3. |
| 3 | [MINOR] `urlsplit` moved inside the `try` without a comment | **FIX round 1.** One-line comment. |
| R1 | Trusted → Basic → token order not pinned by a test with a credential configured (refuted 1/3 as not a code defect, but the observation is true) | **Added to fix round 1** as a cheap regression test. |
| R2–R5 | Tautological "traceback" assertion; import-time `sys.stderr` binding; unreachable `except AttributeError`; hard-coded 12/4096 in messages | Refuted; no change. |

**Fix round 1 outcome** (2026-09-13T09:09:01Z): Codex registered the ISO-8859-1 and percent-encoded
spellings, added the unquote redaction pass, both comments, the gate-order test and a
raw-socket non-ASCII regression. Visionary re-ran the proof: ruff clean, 293 passed (88
console + 205 invariants; Codex's sandbox again could not bind ports, so the 14 HTTP tests
ran only here), curl `200 401 200`. Visionary's own reproduction then found a residual
variant: a mixed encoding of the password (`/x/p%C3%A4ssw` + raw UTF-8 `örd-abcdefg`)
matches no registered spelling and the printable strip emits `psswrd-abcdefg`. **Fix round
2 (final)** asks for a structural rule: any route containing a non-printable or non-ASCII
character is logged as the fixed text `non-ascii-target`, never as a stripped remainder.
**Fix round 2 outcome — Rock 1 ACCEPTED** (2026-09-13T09:16:15Z): the route field now falls back to
the fixed text `non-ascii-target` whenever the redacted, unquoted route contains a character
outside printable ASCII; the controlled-target sub-case and the raw-socket test (now three
encodings: raw UTF-8, fully percent-encoded, mixed) were updated. Visionary re-ran the proof
(ruff clean, 293 passed, curl `200 401 200`) and a four-encoding raw-socket reproduction: two
`[redacted]`, two `non-ascii-target`, no spelling of the password or its projection present.
Both fix rounds were used; the Rock is accepted without taking the wheel.

## Rock 4 — Visionary review

Timestamp: 2026-09-13T09:48:19Z. Codex built Rock 4 in parallel with Rock 2 (new
`deploy/launchd/render_console_agent.py`, new `tests/test_console_launchd.py`, one
`.gitignore` line). Visionary read both files in full and ran the launchd-free proof parts:
`git add -N`, ruff clean, 8 launchd tests + 205 invariants green, `plutil -lint -` OK,
rendered plist has absolute paths, one root for environment and logs, no secret. The
launchd bootstrap/health/bootout cycle is deferred until Rocks 2-3 are accepted (it would
exercise a half-built console). Adversarial diff review confirmed 3 of 7 findings, all one
defect:

| # | Finding | Response |
| --- | --- | --- |
| 1-3 | [MAJOR] The artifact root is the one path input never absolutized: a relative `--cron-tracking-root` or `$DCA_CRON_TRACKING_ROOT` renders relative log paths and env root, and `--install` mkdirs under the invoking cwd while the console resolves against the checkout; an empty flag becomes `Path('.')` and overrides the env | **FIX round 1.** Parse the flag as text, empty means unset, absolutize lexically through the existing helper; regression test with a relative flag, a relative env value, and an empty flag. |
| R1-R3 | `--port 0` accepted for a persistent agent | Refuted: mirrors the console's own documented guard; no change. |
| R4 | `test_renderer_fails_closed` asserts only exit 2 | Refuted: the positive-path tests would catch a wrong rule firing; no change. |

**Fix round 1 outcome — Rock 4 ACCEPTED (code)** (2026-09-13T09:50:05Z): the root is parsed as
text, empty means unset, and is absolutized lexically through the same helper as the other
path inputs; regression test covers a relative flag, a relative env value and an empty flag
from a changed cwd. Visionary verified from a scratch directory (relative flag → absolute
under that directory; empty flag with env set → env root), ruff clean, 9 launchd + 205
invariants green. The launchd bootstrap/health/bootout proof runs after Rock 3 (clean
SIGTERM) lands.

## Rock 2 — Visionary review

Timestamp: 2026-09-13T10:02:19Z. Codex built Rock 2 (`dca_console.py` +352/-52, `tests/test_dca_console.py`
+304, 13 new tests, exactly the one permitted existing-test edit). Its sandbox could not bind
ports; the Visionary read the full diff and ran the proof: ruff clean, all console + invariants
tests green, LAN bind banner with the plaintext warning, curl `200 401 200 403`, stop step exit
143 (Rock 3 pending), `--bind 0.0.0.0` refused with exit 2. Adversarial diff review confirmed
**zero** of 3 findings; the Visionary adopted two nits directly: reworded the Sec-Fetch-Site
whitespace-strip comment (the allow-list is the load-bearing check, the strip only normalizes)
and recorded the `127.0.0.2` alias-bind usability gap in `DEFERRED.md`. **Rock 2 ACCEPTED.**
The phone-on-wifi check is an operator step listed for commit approval.
editorial (2026-09-13T10:23:06Z): Rock 4 proof health poll widened from 10 s to 30 s — a cold launchd start of the console takes longer than 10 s because startup probes optional packages in subprocesses; the agent was running with state = running when inspected
**Rock 4 launchd cycle — ACCEPTED** (2026-09-13T10:23:22Z): with Rock 3 in the tree the Visionary ran
the full Rock 4 proof: `--print | plutil -lint -` OK, `--install`, `bootstrap` → `state =
running` with a pid, `/api/health` 200 with `received_host`/`forwarded_host`, hosted console
401 without credentials and 200 with them, `bootout` → `Stopping local console.` in
`launchd.out.log`, port released, agent unloaded; `launchd.err.log` holds exactly one
rate-limited auth-failure line from the credential-less probe. The installed plist was then
removed so the machine carries no login agent until commit approval; the operator installs
it with the renderer as the acceptance checklist describes.

## Rock 3 — Visionary review

Timestamp: 2026-09-13T12:22:52Z. Codex built Rock 3 (`dca_console.py`, `console/app.js`,
`tests/test_dca_console.py`, six new tests). Visionary read the full diff and ran the proof:
ruff clean, 107 console tests green, `node --check` OK, SIGTERM → `Stopping local console.`,
`exit=0`, port released. Adversarial diff review confirmed **zero** of 5 findings (seven
verifier agents were lost to the operator's session limit, leaving two findings unvoted).
Visionary assessed the unvoted ones directly: the calendar-order signature eviction is
refuted (every poll re-stamps `generated_at`, so write-skipping never engaged before or
after); the SIGTERM-between-create-and-start race is real but microsecond-wide, and the
Visionary closed it with a two-line `is_alive()` guard on the shutdown `join` (console
tests re-run green). **Rock 3 ACCEPTED**; the open-tab job-button check after
`kickstart -k` remains an operator step.

## Whole-cycle verification

Timestamp: 2026-09-13T12:29:09Z. `ruff check .` clean. Full suite with CI's gate
(`--cov-fail-under=90`): 8,790 passed, 4 skipped, coverage 92.90%, **3 failed** — all in
`scripts/tests/test_compute_schedule.py::test_compute_cadence_*`, which no file in this cycle
touches; the same three fail identically on a pristine `origin/main` worktree
(`0 0 * * *` vs expected `0 3 * * *`), so they are pre-existing. `scripts/tests` shows the
same three and nothing else. `run_dca.py --demo --report` smoke run passes. Launchd cycle,
LAN curl sequence and SIGTERM stop were proved in-cycle (see the Rock entries above).
Remaining operator steps: phone on the home wifi against the foreground LAN bind; the
open-tab job button after `kickstart -k`; and the Phase B acceptance checklist after
installing Tailscale or cloudflared.

## Rock 5 — Visionary review

Timestamp: 2026-09-13T12:36:26Z. Codex built Rock 5 (`docs/local_console.md` +346, rows in
`CLAUDE.md`, `docs/repo_map.md`, `docs/README.md`, `MEMORY.md`). Visionary read every diff
and ran the proof (`DOCS-OK`, all listed suites green). Adversarial docs review confirmed
2 of 8 findings; the Visionary applied them directly (docs only): the remote-access rule now
says every `--trusted-host` value requires `--auth-file` (not only "external" names) and the
flag paragraph states the same dependency; `MEMORY.md`'s pointer to the replaced
verification record now targets `HANDOFF.md`. Two refuted-but-true wording nits were also
fixed: the checklist's "one-liner above" now says "generator block above", and
`DEFERRED.md` describes the auth-failure line as rate-limited per server. Proof re-run:
`DOCS-OK`. **Rock 5 ACCEPTED.**
