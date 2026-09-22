# Sentinel's Journal

## 2024-05-18 - Unsanitized HTTP Error Response Logging

**Vulnerability:** HTTP response errors from Pipedream containing `PIPEDREAM_TOKEN` (or generic `Bearer` tokens) were directly logged to the application console without being sanitized.
**Learning:** Raw responses from failed external API integrations often echo authentication parameters. Blindly logging these error responses can lead to credential exposure within error tracking logs.
**Prevention:** Implement sanitization helpers across API clients that truncate long response payloads and actively mask standard token patterns prior to application logging.

## 2024-05-24 - Insecure Jinja2 Template Instantiation (XSS Risk)

**Vulnerability:** Jinja2 `Template` was used without `autoescape=True`, leading to potential XSS through injected variables.
**Learning:** `jinja2.Template` defaults to `autoescape=False`. This is a common pitfall.
**Prevention:** Always use `jinja2.Environment(autoescape=True).from_string(...)` when rendering user-supplied or external data into HTML templates.

## 2024-07-14 - XSS in HTML Email Template

**Vulnerability:** The HTML email template embedded external headline texts directly into an HTML string via f-string formatting without escaping it, exposing the system to Cross-Site Scripting (XSS).
**Learning:** External API content is untrusted data and can contain malicious payloads (like `<script>` tags). When interpolating this data into an HTML context, it must always be sanitized or escaped.
**Prevention:** Use standard escaping functions (like `html.escape()`) when interpolating untrusted variables directly into HTML templates, or prefer secure template engines (like Jinja2) that escape variables by default.

## 2026-07-17 - Remove hardcoded Robinhood account number

**Vulnerability:** A hardcoded account number `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER = "<REDACTED>"` was found in `near_close_dca.py`.
**Learning:** Hardcoded secrets/identifiers violate the repository's secrets policy and can be used to identify/compromise broker accounts.
**Prevention:** Use environment variables (e.g. `os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER")`) to inject sensitive identifiers at runtime instead of hardcoding them in the source code.

## 2024-05-24 - Do not commit .coverage files

**Vulnerability:** Binary build and testing artifacts were included in Git.
**Learning:** Checking in test coverage metadata (`.coverage` from pytest-cov) inflates repository size and guarantees continuous merge conflicts across the team.
**Prevention:** Always verify `git status` precisely before committing. Configure `.gitignore` to explicitly reject `.coverage` and similar tool artifacts.

## 2026-07-20 - SSRF and Linter Supression

**Vulnerability:** Found an SSRF vulnerability where `urllib.request.urlopen` lacked URL scheme validation and could be abused via user-supplied webhooks to perform local file inclusion using `file://`.
**Learning:** Adding validation before the call mitigates the threat. However, security linters (like Ruff enforcing `S310`) analyze the syntax, not the surrounding control flow validation. Changing the `# noqa` comment to `# nosec` caused CI failures because Ruff expects `# noqa: S310`.
**Prevention:** Always validate URL schemes (e.g., `url.lower().startswith(...)`) for user-provided URLs passed to `urlopen`. Crucially, preserve existing linter `# noqa: <code_id>` suppression directives exactly as they are when adding functional validations to prevent unintended CI breakage.

## 2024-08-15 - API Key Leakage in Error Logs (URL Parameters)

**Vulnerability:** The `apiKey` parameter was passed in the URL query string when making requests to NewsAPI via the `requests` library. When a `requests.RequestException` was thrown (e.g. 401 Unauthorized), the `requests` library's exception message included the full request URL, which was then logged, leaking the API key to application logs.
**Learning:** Passing credentials via query string parameters exposes them to leakage in multiple places: request logs, proxy logs, and application error logs whenever exceptions containing the URL are stringified.
**Prevention:** Always pass API keys and other credentials in HTTP headers (like `Authorization` or `X-Api-Key`) rather than query parameters. Additionally, sanitize exception messages (`str(exc).replace(api_key, "***")`) before logging them as defense-in-depth against underlying libraries that might still echo sensitive data.

## 2026-07-26 - XSS in Python f-string HTML Templates

**Vulnerability:** The email report template in `sentiment_cron.py` dynamically constructed HTML using f-strings and interpolated unsanitized variables (`ticker` and `company`).
**Learning:** When rendering HTML without a proper templating engine (like Jinja2 with autoescaping), raw variables fetched from external sources or user input can introduce XSS vulnerabilities.
**Prevention:** Always sanitize variables interpolated into HTML f-strings using `html.escape()`. Be sure to cast the variable to a string if its type is not guaranteed to avoid `TypeError`.

## 2026-08-10 - SSRF in strategy_monitor.py

**Vulnerability:** `urllib.request.urlopen` is used in `trading-strategies/strategy_monitor.py`; if the target URL ever becomes configurable/untrusted, it could enable SSRF (including `file://` reads).
**Learning:** Static analyzers (Ruff `S310`) flag `urlopen` based on syntax, so add explicit allowlisting (and/or an explicit suppression) when the URL is trusted.
**Prevention:** Keep the endpoint hardcoded to `https://api.pipedream.com/...` (or validate scheme + host allowlist) before calling `urlopen`, and keep `# noqa: S310` on the call site. `urlopen` also follows redirects (preserving `Authorization`) by default, so build the opener with a custom `HTTPRedirectHandler` that refuses to follow them rather than trusting the initial-URL check alone.

## 2026-08-10 - SSRF in strategy_monitor.py (build_opener Handlers)

**Vulnerability:** `urllib.request.urlopen` is used in `trading-strategies/strategy_monitor.py`; if the target URL ever becomes configurable/untrusted, it could enable SSRF (including `file://` reads).
**Learning:** Adding validation before the call mitigates the threat. However, security linters (like Ruff enforcing `S310`) analyze the syntax, not the surrounding control flow validation. When mitigating SSRF/LFI by globally subclassing and blocking `urllib.request.FileHandler` and `urllib.request.FTPHandler`, these protections can be accidentally bypassed if a specific module later uses `urllib.request.build_opener()` to create a local opener (e.g. to disable redirects). `build_opener()` always adds default handlers (including the unsafe `FileHandler` and `FTPHandler`) unless they are explicitly overridden in its arguments.
**Prevention:** Keep the endpoint hardcoded to `https://api.pipedream.com/...` (or validate scheme + host allowlist) before calling `urlopen`, and keep `# noqa: S310` on the call site. `urlopen` also follows redirects (preserving `Authorization`) by default, so build the opener with a custom `HTTPRedirectHandler` that refuses to follow them rather than trusting the initial-URL check alone. Always import and explicitly pass the globally-defined blocking handlers (e.g., `BlockedFileHandler`, `BlockedFTPHandler`) when invoking `urllib.request.build_opener()`.

## 2026-09-08 - [Enforce urllib.request Secure Opener on Extraneous Modules]

**Vulnerability:** `urllib.request.urlopen` lacked SSRF mitigations for user-supplied webhook URLs in `scripts/drift_detection.py`.
**Learning:** Validation methods like `_assert_safe_destination` that pre-filter `http`/`https` schemes do not prevent TOCTOU redirection bugs if the underlying `urllib.request` handles redirects unsafe out-of-the-box.
**Prevention:** `urllib.request.build_opener(..., BlockedFileHandler, BlockedFTPHandler)` must be used comprehensively anytime external network calls are performed by built-in Python standard libraries.

## 2026-09-08 - SSRF in strategy_monitor.py SheetsWriter

**Vulnerability:** `urllib.request.urlopen` lacked SSRF mitigations for operator-supplied Sheets API URLs in `trading-strategies/strategy_monitor.py`, potentially enabling local file inclusion or internal network requests.
**Learning:** Checking the `scheme` and `netloc` components of a URL via `urlparse` is insufficient to prevent SSRF if the URL resolves to an internal or restricted IP address, or if DNS rebinding occurs. We must validate the resolved IP addresses against a denylist of private/local subnets.
**Prevention:** Rely on the centralized `dca_net_utils.assert_safe_destination` utility to validate all operator-supplied URLs before making outbound requests, ensuring both scheme/host checks and IP-level verification are performed.

## 2026-09-08 - [Secure BaseHTTPRequestHandler Against Slowloris]

**Vulnerability:** The local `ThreadingHTTPServer` configured via `BaseHTTPRequestHandler` in `dca_console.py` lacked a socket timeout configuration. This meant connections could hang indefinitely during the header-reading phase.
**Learning:** Adding `self.connection.settimeout()` inside `do_GET` or `do_POST` is insufficient for protection against Slowloris-style attacks, because `socketserver.StreamRequestHandler` parses HTTP headers *before* invoking `do_GET` or `do_POST`.
**Prevention:** Always define a `timeout` class attribute directly on the `BaseHTTPRequestHandler` subclass (e.g., `timeout = 5`) so the underlying socket configuration applies the timeout immediately upon connection and during the vulnerable header-reading phase.
