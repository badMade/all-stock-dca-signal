# Cost Tracking

How automation cost is monitored and controlled. Machine-readable artifact
shape: [`schemas/cost_tracker.schema.json`](../schemas/cost_tracker.schema.json).

## Policy

- **Daily tasks**: compact / local-first. Avoid large narratives on quiet days
  (e.g. the volatility scan alerts only on >5% outliers).
- **Heavy analysis**: reserved for **Fridays and monthly** refreshes.
- **Manual full runs**: avoid unless automation fails.

## Monthly cost review (`bec4a6ee`)

- Runs the first weekday of the month at **9:55 AM ET**.
- Reports **usage proxies** (task-run counts, heavy-analysis counts, manual
  runs, approximate tokens). **No exact credit/billing figures** — exact
  Perplexity credit consumption is not exposed, so do not claim exact credits.

## Proxy metrics

| Proxy | Meaning |
|---|---|
| `task_runs` | Total scheduled task executions in the period |
| `heavy_analysis_runs` | Weekly/monthly full refreshes |
| `manual_runs` | Ad-hoc manual runs (should trend to zero) |
| `approx_tokens` | Rough token usage estimate, if available |

## Levers

- Prefer compact Notion "Latest Run" updates daily; full dashboard only weekly/monthly.
- Keep no-outlier days silent.
- Reuse cached/local data instead of re-fetching whole universes.
