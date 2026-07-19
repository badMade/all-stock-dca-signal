## 2024-05-24 - Insecure Jinja2 Template Instantiation (XSS Risk)
**Vulnerability:** Jinja2 `Template` was used without `autoescape=True`, leading to potential XSS through injected variables.
**Learning:** `jinja2.Template` defaults to `autoescape=False`. This is a common pitfall.
**Prevention:** Always use `jinja2.Environment(autoescape=True).from_string(...)` when rendering user-supplied or external data into HTML templates.

## 2026-07-17 - Remove hardcoded Robinhood account number
**Vulnerability:** A hardcoded account number `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER = "<REDACTED>"` was found in `near_close_dca.py`.
**Learning:** Hardcoded secrets/identifiers violate the repository's secrets policy and can be used to identify/compromise broker accounts.
**Prevention:** Use environment variables (e.g. `os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER")`) to inject sensitive identifiers at runtime instead of hardcoding them in the source code.
