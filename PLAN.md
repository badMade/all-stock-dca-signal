# PLAN — Host the existing DCA console as a private web app

Build plan for the `rocket-fuel/hosted-console` cycle. Vision and non-negotiables live in
`VTO.md`; deferred items in `DEFERRED.md`. This plan is the subject of the Same Page
Meeting and is revised on the record until `VERDICT: SAME PAGE`. **Revision 2** (after
Same Page round 1; the per-finding changes are listed at the end).

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
| `--bind ADDR` | IPv4 literal only (`ipaddress.IPv4Address`), default `127.0.0.1`; foreground use only — the launchd renderer has no `--bind` | `HTTPServer` is AF_INET; hostnames/IPv6 deferred; the persistent service stays loopback-only |
| `--trusted-host HOST[:PORT]` (repeatable) | lowercased; hostname regex; no scheme, path, wildcard or whitespace; port ≤ 65535 | Extra names accepted in `Host`. Also accept `https://` origins (today only `http://`, `dca_console.py:344`, which would block POSTs behind an HTTPS proxy) |
| `hosts(port)` | `{127.0.0.1:port, localhost:port}` ∪ `{bind:port}` when bind is a specific non-loopback IP ∪ `trusted_hosts`; `0.0.0.0` adds nothing | Phase A needs no `--trusted-host`; with `0.0.0.0` the server cannot know which address the client typed |
| Effective authority and `Origin` | The *effective authority* is the accepted `Host`, or under `--behind-proxy` the accepted `X-Forwarded-Host`. When `Origin` is present it must equal exactly `http://<authority>` or `https://<authority>` | Membership of both in allowed sets is not enough: a loopback `Host` with a tailnet `Origin` (or vice versa) must be refused |
| `--behind-proxy` | Additionally accept a **configured** name (never a loopback name) from `X-Forwarded-Host`; requires ≥1 `--trusted-host` **and** a loopback bind | A LAN-reachable socket that honours forwarded headers would let any LAN client forge them |
| Fail-closed matrix (in `__post_init__`) | non-loopback bind without a credential; any `trusted_hosts` without a credential; `behind_proxy` without `trusted_hosts` or with a non-loopback bind; `0.0.0.0` without `trusted_hosts`; malformed bind / host values | Nothing listens on a reachable address without a credential |
| `Sec-Fetch-Site: cross-site` | Rejected everywhere **except** `GET /` with `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. Every `/api/*` route, including GETs, stays cross-site-rejected | Cloudflare Access login is a redirect chain through `<team>.cloudflareaccess.com`; Fetch Metadata computes the site over the whole chain, so the first document load after login arrives `cross-site`. The document route is side-effect-free; `/api/snapshot` and `/api/export` are not (they call `_save_summary()`), so they keep the strict rule. Existing test at `tests/test_dca_console.py:122-124` still gets 403 |
| 403 body | adds `received_host` (≤ 253 chars) and `forwarded_host` | The runbook's "what does the backend see" probe; JSON with `nosniff`, no HTML |
| Brute-force backoff | Deferred (see `DEFERRED.md`); 12-char minimum, documented generator, one stderr line per failure (client IP + path, never credentials) | Behind either proxy every request is from `127.0.0.1`; per-IP state becomes a global lockout lever |
| SIGTERM | Context manager installs a handler that raises `KeyboardInterrupt`, reusing the Ctrl+C teardown (`dca_console.py:492-497`); restores the previous handler; wraps only the serving branch | `server.shutdown()` from the handler would deadlock (same thread as `serve_forever`). Worst-case teardown ≈ 11 s (`dca_console_jobs.py:210-226`), inside the plist's `ExitTimeOut = 30` |
| Handler hygiene | `Handler.timeout = HANDLER_TIMEOUT_SECONDS` (30; today `None`, only POST bodies get `settimeout(5)` at line 400); `version_string()` override | An idle LAN/proxy connection must not pin a thread; `Server:` should not advertise the Python version |
| Long uptime | Cap `ConsoleApp.signatures` to the last 32 days | Everything else is already bounded |
| launchd | `deploy/launchd/render_console_agent.py` renders the plist with `plistlib` (`--print` / `--install`), validates the auth file via `load_auth_file`, refuses without `<checkout>/.venv/bin/python`, resolves **one** artifact root (`--cron-tracking-root` or `dca_paths.cron_tracking_root()`), always writes it into the plist environment and derives the log paths from that same value, creates the log directory, prints the `launchctl` commands. Fixed label `com.all-stock-dca.console`. Optional `--octopus-run DIR` passthrough. No `--bind`. No placeholder template | Same "logic in a script, not the unit" rationale as `deploy/run-drift-check.sh:6-17`; `plistlib` cannot mis-escape; one resolved root means logs and console artifacts can never land in different trees; a fixed label removes an unvalidated path/shell fragment |
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
consulted by `redact()`; one stderr line per failure. `make_server(app, port, access=None)`
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
masks it; `repr(access)` lacks it); `test_auth_failure_is_logged_without_credentials`.

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"; AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_config_invariants.py -q \
&& (.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & sleep 4; \
    curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/health; \
    curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:8765/api/bootstrap; \
    curl -s -o /dev/null -w '%{http_code}\n' -K <(printf 'user = "%s"\n' "$(<"$AUTH")") http://127.0.0.1:8765/api/bootstrap; kill %1)
```
Expected: ruff clean; 84 console passes (78 + 6) and the invariants file green; curl
prints `200 401 200`.

### Rock 2 — `--bind`, `--trusted-host`, `--behind-proxy`, hardened handler (Phase A reachable)

**Scope.** Extend `ConsoleAccess` with `bind`, `trusted_hosts`, `behind_proxy` and move
the whole fail-closed matrix into `__post_init__`; pure `hosts(port)`,
`effective_authority(headers, port) -> str | None` and `trusted(headers, port)`; replace
`_trusted()` (`dca_console.py:339-345`); `Origin` bound to the effective authority with
`http://` or `https://`; cross-site tolerance for `GET /` navigations only; 403 diagnostic
body; `ThreadingHTTPServer((access.bind, port), …)` (line 412); `HANDLER_TIMEOUT_SECONDS
= 30`; `version_string()`; banner (472-473) prints bind, port, trusted hosts and
`auth: basic user=<name>` (never the password); reword the `--port` help (426) and the
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
`test_cross_site_navigation_allowed_only_for_document_route` (GET `/` with
`Sec-Fetch-Site: cross-site, Sec-Fetch-Mode: navigate, Sec-Fetch-Dest: document` → 200;
the same headers on `/api/snapshot` → 403 and no summary file is written; `Sec-Fetch-Mode:
cors` on `/` → 403; POST → 403);
`test_idle_connection_is_closed_by_handler_timeout` (`HANDLER_TIMEOUT_SECONDS = 0.2`, raw
socket that sends nothing gets EOF within 2 s); `test_server_header_does_not_leak_python_version`.

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"; AUTH=~/.config/all-stock-dca/console-auth
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_config_invariants.py -q \
&& LAN=$(ipconfig getifaddr en0) && (.venv/bin/python from.py --local --no-browser --bind "$LAN" --auth-file "$AUTH" & sleep 4; \
    curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/health"; \
    curl -s -o /dev/null -w '%{http_code} ' "http://$LAN:8765/api/snapshot"; \
    curl -s -o /dev/null -w '%{http_code} ' -K <(printf 'user = "%s"\n' "$(<"$AUTH")") "http://$LAN:8765/api/snapshot"; \
    curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' "http://$LAN:8765/api/health"; kill %1) \
&& { .venv/bin/python from.py --local --no-browser --bind 0.0.0.0; echo "exit=$?"; }
```
Expected: ruff clean; 94 console passes (84 + 10); curl prints `200 401 200 403`; the
last command prints the fail-closed message and `exit=2`. Then a phone on the same wifi:
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
&& (.venv/bin/python from.py --local --no-browser --port 8765 --auth-file "$AUTH" & sleep 4; kill -TERM %1; wait %1; echo "exit=$?")
```
Expected: 98 passes; output ends with `Stopping local console.` then `exit=0` within
seconds. Manual (after Rock 4): with a tab open, `launchctl kickstart -k
gui/$(id -u)/com.all-stock-dca.console`, then click "Test DCA pipeline" in the same tab
without reloading → the job starts.

### Rock 4 — launchd agent renderer

**Scope.** `deploy/launchd/render_console_agent.py` (stdlib `argparse`, `plistlib`, `os`,
`pathlib`; `sys.path.insert(1, <checkout>)` per the ticker-advisor precedent, then
`from dca_paths import cron_tracking_root` and `from dca_console import load_auth_file`).
Args: `--auth-file` (required), `--port 8765`, `--trusted-host` (repeatable),
`--behind-proxy`, `--octopus-run DIR` (optional, must be an existing directory),
`--cron-tracking-root DIR` (optional), exactly one of `--print` / `--install`. No `--bind`,
no `--label`. The renderer resolves `ROOT = --cron-tracking-root or cron_tracking_root()`
once and uses it for both the plist environment and the log paths. Rendered dict:

```
Label                = com.all-stock-dca.console
ProgramArguments     = [<checkout>/.venv/bin/python, <checkout>/from.py, --local, --no-browser,
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
```

`.gitignore` gains `deploy/launchd/*.plist` beside the existing `deploy/*.env` line.

**Non-goals.** Running `launchctl` from the script; a `/Library/LaunchDaemons` variant;
a LAN-bound (Phase A) service; installing Tailscale or cloudflared.

**Files.** `deploy/launchd/render_console_agent.py` (new), `tests/test_console_launchd.py`
(new; imports the script via `importlib.util.spec_from_file_location` so coverage counts
it), `.gitignore`.

**Tests.** `test_renders_expected_program_arguments_and_paths` (loopback-only Phase B
form with and without `--trusted-host mac.tailnet.ts.net --behind-proxy` and
`--octopus-run`; `plistlib.loads` output has the exact `ProgramArguments`,
`Label == "com.all-stock-dca.console"`, `KeepAlive == {"SuccessfulExit": False}`,
`RunAtLoad`, `ExitTimeOut == 30`, log paths under `<ROOT>/console`, no
`EnvironmentVariables` key containing `PASSWORD`);
`test_custom_root_is_used_for_both_environment_and_logs` (`--cron-tracking-root <tmp>` →
`EnvironmentVariables["DCA_CRON_TRACKING_ROOT"] == <tmp>` and both log paths start with
`<tmp>/console/`); `test_rendered_plist_contains_no_secret`; `test_renderer_fails_closed`
(loose-mode auth file, `--behind-proxy` without `--trusted-host`, `--octopus-run` that is
not a directory, missing venv python → exit 2; no `--bind` / `--label` arguments exist);
`test_install_writes_agent_and_creates_log_dir` (`HOME` → tmp; 0644; log dir exists).

**Proof.**
```bash
CONSOLE_TESTS="tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py tests/test_console_security_logging.py"; AUTH=~/.config/all-stock-dca/console-auth
git add -N deploy/launchd tests/test_console_launchd.py \
&& .venv/bin/python -m ruff check . && .venv/bin/python -m pytest $CONSOLE_TESTS tests/test_console_launchd.py tests/test_config_invariants.py -q \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --print | plutil -lint - \
&& .venv/bin/python deploy/launchd/render_console_agent.py --auth-file "$AUTH" --install \
&& launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.console.plist && sleep 5 \
&& curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/health \
&& launchctl print gui/$(id -u)/com.all-stock-dca.console | grep -E 'state = |pid = '
```
Expected: tests green (98 console + 5 launchd); `stdin: OK`; `200`; `state = running`
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
  home wifi; use a disposable credential and rotate it before Phase B; never install it as
  the service.
- **Phase B: Tailscale Serve.** Header-echo probe → `tailscale serve --bg
  http://127.0.0.1:8765` → `tailscale serve status` → rotate the credential → re-render
  with `--trusted-host <mac>.<tailnet>.ts.net --behind-proxy` → `launchctl bootout` then
  `bootstrap` (a changed plist is never applied by `kickstart`).
- **Phase B: Cloudflare Tunnel + Access.** `cloudflared tunnel create` → Access
  application + policy **before** `tunnel route dns` (the name is never public with Basic
  alone) → `config.yml` with the ingress to `http://127.0.0.1:8765` **and the mandatory
  terminal catch-all `- service: http_status:404`** → `cloudflared tunnel ingress validate`
  → `cloudflared service install` → rotate the credential → re-render with
  `--trusted-host console.<domain> --behind-proxy` → `bootout` then `bootstrap`. Note the
  expected double prompt (Access, then Basic) and why Basic stays: stdlib cannot verify the
  Access RS256 JWT.
- **Run as a login service (launchd).** Renderer usage, the four `launchctl` commands with
  the bootout-before-bootstrap rule, log locations, `--octopus-run` for forecasts, the
  sleep caveat (the Mac must not sleep).
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
