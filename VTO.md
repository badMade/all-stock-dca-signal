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
