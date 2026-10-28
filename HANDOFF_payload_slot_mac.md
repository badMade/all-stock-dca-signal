# Handoff: run one payload slot on the Mac (first real proof of the courier fold)

Written 2026-10-27 from cloud session https://claude.ai/code/session_01MWod9HM7ZnMMHJvf4UMvRp.
Repo on the Mac: `/Users/geoffreyplymale/Documents/Git/all-stock-dca`. Branch: `main` at or after commit afa39fbf (PR #1976).

## Why this run
The cloud session could not call Robinhood or Codex, so nothing below has run for real. Unit tests pass (ruff clean,
payload, ingest, market-history, snapshot_cycle, config-invariant tests). One live run of one slot proves two things:
1. The payload build still works after the dead-chunk rescue and the chunking fix.
2. The new plan-first half works: a second `codex exec` reads order history and buying power, then `ingest` writes
   recommendation files. This step is OFF by default (`DCA_PLAN_FIRST_INGEST=1` turns it on).

## Read this first: two things that can surprise you
- **Ingest pushes to GitHub.** `config/recommendations.json` has `publish.github_branch.enabled = true`
  (branch `recommendations`, remote `origin`). `dca_rec_publish.py` runs `git push --force origin <commit>:refs/heads/recommendations`.
  With ingest on, a successful run force-pushes that one branch from the Mac. It never touches `main`, and nothing in the
  repo reads it for trading (plan-first is shadow only). If you do not want that for the first run, set `enabled` to
  `false` in a local, uncommitted edit of `config/recommendations.json`, or use `DCA_CONFIG_DIR` to point at a copy.
- **A test build only works inside the slot's window.** The payload rejects a build made before the slot's decision time
  and quotes older than the freshness bound (an after-hours test build is refused on purpose). Run it during market hours,
  1 to 14 minutes after a slot's decision time (09:35, 10:30, 11:30, 12:30, 13:30, 14:30 ET). `DCA_TRADING_DAY` and
  `DCA_PAYLOAD_NOW_ET` only change the planning clock; they cannot make stale Robinhood data fresh.

## Prerequisites
- Mac awake, set to America/New_York, Codex CLI logged in with the `robinhood-trading` MCP (`codex mcp list`).
- Checkout up to date: `git checkout main && git pull`.
- `.venv` exists with dev requirements (`python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`).
- `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` available (the gitignored `.env`, see docs/local_env_file.md). Needed only for the
  ingest half. Never paste the number into a file or a chat.
- `DCA_CRON_TRACKING_ROOT` matching what the live sessions use (the launchd plist sets it; export it for a manual run).

## Run it
Pick the slot whose decision time passed 1 to 14 minutes ago. Example for the 11:30 slot, run between 11:31 and 11:44 ET:

```bash
cd /Users/geoffreyplymale/Documents/Git/all-stock-dca
export DCA_REPO="$PWD"
export DCA_CRON_TRACKING_ROOT="<same value as the launchd plist>"
export DCA_CODEX_BIN="$(command -v codex)"
# Step A: payload only (the proven path). Expect exit 0.
bash deploy/launchd/robinhood_payload.sh 1130; echo "exit=$?"
```

Then the plan-first half, on a different slot's window. Use a new slot because the launcher reuses a fresh payload
and exits before the ingest step, so re-running the same slot builds nothing and ingests nothing:

```bash
DCA_PLAN_FIRST_INGEST=1 bash deploy/launchd/robinhood_payload.sh 1230; echo "exit=$?"
```

Stderr shows each stage. Nothing here places an order.

## What good looks like
| Check | Expected |
| --- | --- |
| Launcher exit status | `0` (a broker or ingest failure never changes it) |
| `timing total seconds=` | about 130 to 250 s for a full universe |
| Payload file | `$DCA_CRON_TRACKING_ROOT/robinhood_payload/<day>/robinhood_payload_<day>_<slot>.json` plus `.meta.json` |
| `python -m scripts.robinhood_payload check --plan <plan>` | exit 0 (use the same slot) |
| Quotes | now carry `bid_price` and `ask_price` |
| Ingest line | JSON like `{"status": "OK", "slot": ..., "orders": N}` |
| Recommendation files | `$DCA_CRON_TRACKING_ROOT/recommendations/<day>/rec_<slot>_<stamp>.json` and `expected_<slot>_<stamp>.json` |
| Publish lines | `robinhood_payload: publish github_branch ok=True ...` (if left enabled) |

## If it fails
| Symptom | Likely cause and next step |
| --- | --- |
| `no slot is due now; skipping` | Wrong time or Mac clock not on ET. Pass the slot explicitly, run inside the window. |
| `refused` / coverage below 95% | Read `<payload>.refused.json` and `<payload>.chunk_logs/` beside the payload. |
| `payload built ... is too old to ingest` (exit 6/2) | Ingest ran more than 10 minutes after the build, or the broker reply is stale. Re-run inside the window. |
| `another ingest holds the lease` (exit 3) | A previous run's lease is live; wait for it to expire or find the stuck process. |
| `broker worker reported: ...` | The Codex worker could not copy every order page. This is the known risky step; send the `broker.log` text (it is in the launcher's temp folder, deleted on exit, so re-run with a longer look) back to Claude. |
| `plan-first ingest did not complete (status N)` | Best-effort by design. The payload is fine; ingest needs a look. |
| `spread_unavailable` for every order | Quotes lacked `bid_price`/`ask_price`; check a published ticker's `quote`. |

Do not "fix" a failure by skipping tests or lowering the 95% floor.

## After a good run
1. Scheduled use: add `<key>DCA_PLAN_FIRST_INGEST</key><string>1</string>` to the `EnvironmentVariables` of
   `~/Library/LaunchAgents/com.all-stock-dca.robinhood-payload.plist`, then `launchctl unload` and `load` it.
   (The template does not set it; the repo default stays off on purpose.)
2. Confirm no old courier or publish agent is still loaded: `launchctl list | grep all-stock-dca`. The separate
   recommendations-publish agent was removed from the repo in #1976; unload a leftover copy if present.
3. Tell Claude the result (paste the stderr from the run, with no account numbers) so docs can drop "default off".

## Rollback
Unset `DCA_PLAN_FIRST_INGEST` (or remove it from the plist) and reload the agent. The payload path is unchanged and
independent. If the `recommendations` branch got pushed and you do not want it: `git push origin --delete recommendations`.

## Context
- Merged work: #1970 (deprecation notes), #1976 (the fold: `ingest`, `broker-prompt`, bid/ask quotes, launcher step, tests,
  docs), #1980 (handoff cleanup). Docs: `docs/robinhood_payload.md` ("Plan-first ingest"), `docs/plan_first_recommendations.md`.
- Open from earlier: PR #1968 (docstring change plus rebuilt `backup_planner.pyz`) still needs merge-or-close.
