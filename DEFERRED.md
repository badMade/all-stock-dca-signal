# Deferred backlog — hosted DCA console

Items raised during the `rocket-fuel/hosted-console` cycle and deliberately left out of
it. Each entry says why it was deferred so the next cycle can re-evaluate rather than
re-derive.

## Security / access

- **Per-client brute-force backoff.** Deferred because behind either Phase B front
  (Tailscale Serve, cloudflared) every request reaches the console from `127.0.0.1`, so
  per-IP state degenerates into a global lockout lever a remote party could pull. The
  right shape: keyed by `X-Forwarded-For` only under `--behind-proxy`, bounded dict,
  delay rather than lockout. Mitigation in place: 12-character minimum password, a
  documented `secrets.token_urlsafe(24)` generator, and a per-server stderr line rate-limited
  to one per 5-second interval with a suppressed count.
- **Proxy identity in the banner.** Show `Tailscale-User-Login` /
  `Cf-Access-Authenticated-User-Email`. Verifying `Cf-Access-Jwt-Assertion` needs RS256
  plus a JWKS fetch, which the stdlib-only rule forbids, so identity would be
  display-only.
- **Multi-user / roles** (read-only vs can-run-jobs). Today one shared credential and one
  operator.
- **Tailscale ACL / Cloudflare Access device-posture examples** restricting which devices
  may reach the console.
- **In-process TLS** via stdlib `ssl` (self-signed or operator-supplied certificate).
  This is the proper fix for Phase A's plaintext LAN HTTP; this cycle bounds that risk
  instead (foreground only, disposable credential, rotation before Phase B) because the
  operator chose LAN-first for build/test. **HSTS** when served over HTTPS.
- **Customisable launchd label.** Killed in Same Page round 1: arbitrary label text became
  both a plist path component and copy-pasted shell text without validation. The label is
  fixed at `com.all-stock-dca.console`.

## Server

- **Loopback alias binds.** `--bind 127.0.0.2` (any 127.x other than 127.0.0.1) constructs
  without a credential but `hosts()` accepts only `127.0.0.1`/`localhost`, so the console
  refuses its own advertised URL with a 403 (macOS refuses the bind outright). Fail-closed,
  off-host unreachable, and outside the launchd path, so left as a usability follow-up
  (Rock 2 review).
- **IPv6 / hostname `--bind`.** `HTTPServer` is AF_INET; an IPv4 literal is enough for
  this cycle.
- **SIGHUP reload of the auth file** so rotation does not need `launchctl kickstart -k`.
- **`server_bind` override** to skip `socket.getfqdn` if LAN startup ever proves slow on
  a network with broken reverse DNS.
- **Pruning** `<console-output>/jobs/*.json` and `summaries/` beyond N days;
  `newsyslog` rotation for `launchd.out.log` / `launchd.err.log`.
- **Linux systemd unit** for the console (this cycle targets the Mac that holds the
  artifact tree; `deploy/systemd/` covers only the drift checks).

## Content (explicitly out of scope this cycle)

- **3D market graph route.** `viz/` assets plus `dca_market_graph.build_payload_json`
  already exist; serving them per request beats inlining 608 KB of three.js.
- **Weekly performance dashboard panel.** `dca_weekly_dashboard` builders return
  markdown strings, so this needs a renderer or a refactor to structured data.
- **Alert digest view** over `scripts/alert_digest.py`.
