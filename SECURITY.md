# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**, not in a public issue or pull request:

- Use GitHub's private vulnerability reporting for this repository (Security tab → "Report a vulnerability"), or
- contact the repository owner, `@badMade`, through the contact details on their GitHub profile.

Include what you found, how to reproduce it, and what you think the impact is. Do not include real
account numbers, tokens, webhook URLs, or broker credentials in a report; describe where one is exposed
instead.

This is a single-operator project, so there is no formal response SLA. The target is to acknowledge a
report within 7 days.

## Scope

In scope: anything in this repository that could cause an unintended brokerage order, leak a secret
(account number, token, webhook URL), bypass a safety gate (`dry_run`, `enable_live_orders`, budget caps,
the 7-day kill switch), or let an untrusted input change behaviour (for example a crafted broker/MCP
response, config file, or workflow input).

Out of scope: the operator's brokerage account itself, third-party services (Robinhood, Slack,
Healthchecks, Notion), and the research trees (`trading-strategies/`, `analysis/`), which carry their own
dependencies and are not part of the live path.

## What this repository does not contain

No credentials. The brokerage account number lives only in `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` and is
referenced in config as `"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"`. The runtime is standard-library only. See
[`CLAUDE.md`](CLAUDE.md) "Hard invariants" and [`docs/safeguards.md`](docs/safeguards.md).

If you find a secret-shaped value committed anywhere (including git history), report it privately so it
can be rotated before anything is made public.

## Supported versions

Only the current `main` branch is supported.
