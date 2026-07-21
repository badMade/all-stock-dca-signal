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
