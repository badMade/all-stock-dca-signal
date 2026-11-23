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

## 2026-10-04 - Response Redaction on Document Serving Endpoints

**Vulnerability:** Documentation files and live executor prompts served via `/api/docs?path=...` returned raw bytes without passing through `redact_value()`, potentially revealing account numbers, registered secrets, sensitive environment variables, or local user paths.
**Learning:** Even when files are checked into version control under strict invariant checks, dynamic operator prompts and documentation pages can contain sample accounts or local filesystem paths that should never be broadcast over HTTP.
**Prevention:** Enforce defense-in-depth output redaction (`redact_value`) at the HTTP handler boundary for all served documentation text.

## 2026-10-23 - Parameter Pollution via Duplicate JSON Keys (CRITICAL)

**Vulnerability:** Standard `json.loads` in `dca_console.py` POST endpoints silently resolved duplicate keys by keeping the last occurrence. If an attacker supplied a payload like `{"action": "valid_job", "action": "malicious_job"}`, initial routing or validation could see one value while execution triggered on the other, bypassing safety checks.
**Learning:** `json.loads` allows parameter pollution when accepting untrusted API payloads unless explicitly protected.
**Prevention:** Always use `json.loads(body, object_pairs_hook=_reject_duplicate_keys)` when parsing safety-bound configs or untrusted POST requests to explicitly raise `ValueError` on duplicate JSON keys.

- Testing (Pytest Discovery): When refactoring or renaming tests, ensure the function names continue to start with `test_`. Removing the underscore (e.g., `testproxy_...`) will cause Pytest to silently skip the test during discovery without raising an error.
- Workflow (Knowledge Retention): At the end of a testing or debugging task, append any new test isolation insights or scalar boundary rules to `.jules/sentinel.md` or `.jules/bolt.md` (using `>>`) to maintain team knowledge and prevent architectural drift.

## 2024-11-20 - Boolean Coercion Bypass in Validation

**Vulnerability:** Unbounded numeric parsing missed an explicit `bool` check before checking `isinstance(value, (int, float))`.
**Learning:** In Python, `isinstance(True, int)` evaluates to `True`, which can bypass safety invariants (like `pending_usd`).
**Prevention:** Always guard against booleans explicitly (`not isinstance(v, bool)`) before evaluating `isinstance(v, (int, float))`.

## 2026-10-31 - [Harden float conversion against OverflowError]

**Vulnerability:** Converting untrusted query strings via `float()` can raise an unhandled `OverflowError` on astronomically large inputs (like `10**1000` from a JSON payload or a padded URL string). This bypasses downstream `math.isfinite` validation and throws a 500 error, enabling Denial-of-Service vectors.
**Learning:** While `math.isfinite()` protects against parsed `inf` and `nan`, the underlying `float()` conversion of an oversized Python integer (produced by standard `json.loads` or custom string slicing) will `raise OverflowError` before `math.isfinite()` even executes.
**Prevention:** Always wrap `float()` conversion of external strings inside a `try...except (ValueError, TypeError, OverflowError)` block and substitute a fail-closed sentinel (like `math.nan`) to guarantee deterministic validation flow.

## 2026-11-08 - Fail-Closed Validation for Sizing Suggestion Readers

**Vulnerability:** `sizing_adjustment_reader._read_if_fresh()` purported to mirror `dca_config.load_ml_sizing_adjustment()`, but omitted critical defense-in-depth checks: it accepted duplicate JSON keys, non-finite (`NaN`, `Inf`) and boolean (`True`/`False`) multiplier adjustments, unhandled `AttributeError` on non-dict JSON, and crashed with unhandled `TypeError` on offset-naive timestamps and unhandled `OverflowError` on oversized integer payloads.
**Learning:** Documenting that a helper "mirrors" another module's hardened parsing does not guarantee parity unless all failure modes (type coercion, non-finite floats, duplicate keys, timezone awareness, future timestamps) are actively tested and enforced. In Python, `isinstance(True, int)` and `datetime.fromisoformat()` parsing naive datetimes will silently bypass loose numeric and date arithmetic.
**Prevention:** Always pair `json.loads(..., object_pairs_hook=_reject_duplicate_keys)` with explicit `isinstance(val, bool)` guards, `math.isfinite()`, timezone offset checks (`tzinfo is not None`), and comprehensive exception trapping `(json.JSONDecodeError, KeyError, ValueError, TypeError, OverflowError, OSError)` for every external or persisted configuration reader.

## 2026-11-15 - Fail-Closed OverflowError Handling in Goals Subsystem Validation

**Vulnerability:** `dca_goals._number()` parsed JSON numeric parameters using bare `float(value)` prior to `math.isfinite()`. An oversized integer in `goals.json` or an `/api/goals/propose` HTTP POST payload raised an unhandled `OverflowError` during `float()` conversion, bypassing `math.isfinite()` and crashing both `dca_goals.load_goals()` (breaking its documented contract to never raise into a scheduled slot) and the console HTTP handler with an unhandled 500 error instead of failing closed to neutral goals or returning a 400 validation response.
**Learning:** Hand-crafted JSON schema validators that accept `(int, float)` must explicitly anticipate arbitrary-precision Python integers parsed from valid JSON. While `math.isfinite()` safely rejects `NaN` and `Infinity`, converting an oversized integer via `float()` raises `OverflowError` before `math.isfinite()` is ever evaluated, and `OverflowError` inherits from `ArithmeticError` rather than `ValueError`.
**Prevention:** Wrap `float()` coercions inside a `try ... except OverflowError` block and explicitly raise `ValueError(f"{name} must be finite")` so downstream callers (`load_goals`, `validate_goal_proposal`, and web handlers) cleanly catch the validation failure and fail closed deterministically.


## 2026-11-16 - Reject Duplicate JSON Keys and Oversized Ints in Primary Execution Gates
**Vulnerability:** `scripts/primary_gates._load_orders` parsed safety-bound `orders_pages` JSON envelopes with bare `json.loads(...)` instead of `object_pairs_hook=dca_config._reject_duplicate_keys`, allowing duplicate `"pages"` or row keys to overwrite prior order history and bypass Gate 4 (`gate4` rogue/compatible interlock) or Gate 6 (`budget` cap arithmetic); additionally, `_usable_buying_power` called `math.isfinite()` on arbitrary `int` inputs without catching `OverflowError`, and `_cmd_budget` swallowed `_EnvelopeRefused` under `except ValueError:`.
**Learning:** While `scripts/backup_planner.py` and `scripts/merge_order_pages.py` enforced `_reject_duplicate_keys` on `orders_pages` envelopes, the primary gate CLI added later omitted `object_pairs_hook` on the same artifact and relied on `math.isfinite(int)` without an `OverflowError` guard.
**Prevention:** Always pass `object_pairs_hook=dca_config._reject_duplicate_keys` whenever loading safety-bound broker envelopes (`orders_pages`), guard `math.isfinite()` against `OverflowError` on untrusted numeric inputs, and catch `_EnvelopeRefused` before `ValueError` in CLI handlers.


## 2026-11-17 - Harden Placement-Boundary Candidate Refresh Against Booleans, Non-Finite Lows, Oversized Ints, and Duplicate JSON Keys
**Vulnerability:** `scripts/refresh_market_open_candidates.py` checked `isinstance(price, bool)` on refresh quotes, but parsed `prior_close`, `prior_low`, `selected_base_amount_usd`, and `desired_usd` with bare `float(...)` without rejecting booleans or catching `OverflowError`, omitted `math.isfinite()` / positivity validation on `prior_low` (allowing `prior_low=Infinity` to force `price < prior_low` and inflate base sizing), and loaded `--candidates` / `--quotes` with bare `json.loads()` without `object_pairs_hook=_reject_duplicate_keys`.
**Learning:** Guarding only the first numeric field (`price`) against boolean coercion while leaving sibling pricing/sizing fields (`prior_close`, `prior_low`, `selected_base_amount_usd`, `desired_usd`) on bare `float()` creates an inconsistent validation boundary where corrupt or duplicate-keyed JSON inputs can bypass placement-time size caps or crash the batch with `OverflowError`.
**Prevention:** Centralize positive finite numeric parsing in a single helper that rejects `bool`, catches `(TypeError, ValueError, OverflowError)`, and enforces `math.isfinite(v) and v > 0` across all price and sizing fields, and always pass `object_pairs_hook=_reject_duplicate_keys` when loading candidate and quote JSON payloads.

## 2026-11-18 - Harden Same-Day Top-Up Gate Against Duplicate JSON Keys, Booleans, and Oversized Ints
**Vulnerability:** `scripts/same_day_top_up_gate.py` parsed `--orders-pages` and `--candidates` via bare `json.load()` without `object_pairs_hook=_reject_duplicate_keys` (allowing duplicate `"pages"` or candidate keys to overwrite prior same-day order history or desired amounts), lacked a mapping guard on candidate array elements, allowed boolean coercion (`True -> 1.0`) and unhandled `OverflowError` in `_f()`, and omitted `math.isfinite()` / `OverflowError` / type checks in `_resolve_minimum_order_usd()`.
**Learning:** While Gate 6 (`scripts/primary_gates.py`) and Gate 5 refresh (`scripts/refresh_market_open_candidates.py`) enforced duplicate-key rejection and boolean/finiteness/overflow guards, Gate 6.5 (`scripts/same_day_top_up_gate.py`) relied on bare `json.load()` and `float()` conversion without `isinstance(value, bool)` or `OverflowError` handling.
**Prevention:** Always pass `object_pairs_hook=_reject_duplicate_keys` when parsing safety-bound `--orders-pages` and `--candidates` files, reject `bool` and catch `OverflowError` before `float()` in numeric parsers, and validate candidate array items as `Mapping` instances before field access.


## 2026-11-19 - Harden Morning Circuit-Breaker Loaders Against Duplicate JSON Keys, Booleans, and Oversized Ints
**Vulnerability:** `morning_dca.load_max_per_order_usd()` and `morning_dca.load_max_orders_per_day()` parsed `config/budgets.json` with bare `json.loads()` instead of `object_pairs_hook=_reject_duplicate_keys` (unlike sibling `load_max_consecutive_placement_failures()`), allowing duplicate `"circuit_breakers"` or cap keys to silently raise spending/order-count limits via last-one-wins across the entire `MorningDCAExecutor` hierarchy (`morning`, `market_open`, `midday`, `intraday`). Additionally, `load_max_per_order_usd()` used bare `float(raw_val)` without rejecting `bool` (`True` silently coerced to `$1.00`), non-numeric strings, or catching `OverflowError` on oversized integers.
**Learning:** Hardening only one of three adjacent `budgets.json` circuit-breaker loaders (`load_max_consecutive_placement_failures`) left its two siblings (`load_max_per_order_usd` and `load_max_orders_per_day`) with weaker parsing semantics than `dca_config` and `dca_runner` on the exact same config file.
**Prevention:** Enforce `object_pairs_hook=_reject_duplicate_keys` across all `budgets.json` safety-cap loaders, reject `bool` and non-`(int, float)` values before `float()`, and catch `OverflowError` alongside `(TypeError, ValueError)` to guarantee fail-closed `ValueError` / `RuntimeError` semantics.


## 2026-11-19 - Harden dca_config Numeric Loaders Against Booleans, Non-Finite Floats, Strings, and Oversized Ints
**Vulnerability:** `dca_config._config_float()` accepted strings (`isinstance(raw, (int, float, str))`) and called bare `float(raw)` without catching `OverflowError` on oversized JSON integers; `dca_config._reject_bool_numeric()` omitted `not isinstance(raw_value, (int, float))` and `OverflowError` handling; `load_sizing_tiers()` and `load_per_ticker_sizing()` used bare `float(...)` on `min_drop_pct`, `multiplier`, `base_amount_usd`, and `base_amount_below_close_usd` without rejecting booleans or non-finite values (`NaN`/`Infinity`) and did not check `math.isfinite()` on `minimum_order_usd`; and `load_ml_sizing_adjustment()` called `math.isfinite(max_staleness_hours)` on arbitrary ints without catching `OverflowError` (violating its documented fail-closed `{}` contract).
**Learning:** Partial numeric hardening in `dca_config` left `_config_float` and `_reject_bool_numeric` vulnerable to `OverflowError` on arbitrary-precision JSON ints and string coercion, while `load_sizing_tiers` and `load_per_ticker_sizing` bypassed `_reject_bool_numeric` and `math.isfinite` entirely on tier thresholds, multipliers, and per-ticker base amounts.
**Prevention:** Require `not isinstance(v, bool) and isinstance(v, (int, float))` and wrap `float(v)` in `try ... except OverflowError` inside central numeric helpers (`_config_float`, `_reject_bool_numeric`), route all ladder and per-ticker sizing fields through those helpers with explicit `math.isfinite()` checks, and guard `math.isfinite()` against `OverflowError` in fail-closed loaders.


## 2026-11-22 - Harden Self-Heal Schedule Sync Against Cron/YAML Injection and Non-Atomic Writes
**Vulnerability:** `scripts/compute_schedule.py` interpolated `schedule` and `rationale` directly into `.github/self-heal-schedule.yml` (`_old_update_schedule_file`) and `.github/workflows/self-heal.yml` (`sync_workflow_cron`) without validating that `schedule` was a single-line 5-field POSIX cron expression or escaping single quotes/newlines in `rationale`; `update_schedule_file()` also staged temp files in `/tmp` via `shutil.move` without cleanup on YAML validation failure.
**Learning:** An `override: true` entry in `.github/self-heal-schedule.yml` (or a malformed rationale) flows directly into `.github/workflows/self-heal.yml`, which runs with `contents: write`, `pull-requests: write`, and `issues: write`. Using `\Z` (not `$`) and explicit ASCII spaces (not `\s`) in the cron regex is essential because Python's `$` and `\s` accept trailing or embedded newlines.
**Prevention:** Enforce `_validate_cron_schedule()` (`\A...\Z`, `re.ASCII`) before any write to `.github/self-heal-schedule.yml` or `.github/workflows/self-heal.yml`, sanitize newlines and single quotes (`'` -> `''`) in `_old_update_schedule_file`, and write `update_schedule_file()` atomically via a same-directory temp file with `finally` cleanup and `os.replace()`.
