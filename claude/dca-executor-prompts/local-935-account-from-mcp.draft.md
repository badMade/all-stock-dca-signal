# DRAFT prompt edits: resolve the account from the Robinhood MCP (local 9:35 task)

Applies to `~/.claude/scheduled-tasks/935-dca-local/SKILL.md`. Not applied anywhere. Two edits plus one
search-and-replace rule. Rationale: a GUI-launched desktop app does not inherit the shell profile, so
`ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` is never set and Gate 2 check 5 aborts every fire. Every repo script
(`assemble_order_pages --account`, `same_day_top_up_gate --account`, `dca_config.expand_config_env`) already
accepts the value from the environment or a flag, so one MCP lookup, exported per command, covers all of them.

## Edit 1 -- replace Gate 2 check 5

OLD:
> 5. `[ -n "$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" ] && echo account-env-set || echo account-env-missing`. ... abort `Failing gate: Gate 2` with "account number environment variable not visible to the executor shell".

NEW:
> 5. ACCOUNT FROM THE MCP. Call `get_accounts` (read-only). Require EXACTLY ONE account with `agentic_allowed` true
>    and `state` active, and `deactivated` false. Zero or more than one is an abort: `Failing gate: Gate 2` with
>    "could not resolve exactly one tradable Agentic account from the Robinhood MCP". Hold that account's
>    `account_number` in memory as ACCT. If `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` is also set in the shell and
>    differs from ACCT, abort the same way ("environment account number disagrees with the MCP's tradable
>    account"): a mismatch means one of the two is stale and nothing may trade on a guess. Print only the
>    last four digits of ACCT, never the full value (SECRET RULE). Gate 2 check 5 PASSES on a single match.

## Edit 2 -- add to the SECRET RULE and to "LIVE EXECUTOR"

> ACCT is read from `get_accounts` at Gate 2 and used for every MCP call that takes `account_number` and every
> `--account` flag. It is never written to an artifact, alert, message-center post, or any file; the only places
> it appears are tool arguments and the per-command environment prefix below.

## Rule 3 -- replace every `${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}` with ACCT, and prefix repo commands

1. In every MCP call (`get_equity_orders`, `get_portfolio` for buying power, `place_equity_order`), pass `account_number` = ACCT.
2. In every `--account ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}` flag, pass ACCT.
3. Shell state does not persist between calls, so start each repo command that loads config or touches the account
   with the export on the same line, never in a file or a profile:
   `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER=<ACCT> python3 -m scripts.<module> ...`
   Needed for: `generate_market_open_candidates`, `prior_reference_bindings`, `assemble_order_pages`,
   `same_day_top_up_gate`, `finalize_run_evidence`, and any `python3 -c` that imports `dca_runner`/`dca_config`.
4. The saved `orders_pages` JSON `"account_number"` field is the one place the gates demand the real value
   (the top-up gate rejects an account mismatch). Write ACCT there; this file lives under the gitignored
   `cron_tracking/` tree, so it never leaves the machine. Do not copy it into alerts.
5. Drop `DCA_CRON_TRACKING_ROOT` from the "must be set" language: unset is fine; keep only the report line.

## Test-mode check before enabling

Run the Gate 2 sequence with the MCP lookup in the test-mode block (no order tools). Expect check 5 PASS with a
last-four-digits line, then Gate 4's `get_equity_orders` using ACCT.

## Caveat to decide

`get_equity_orders`'s own description says "never default from get_accounts". Your instruction overrides that
for this task, but the tool text may make a future session hesitate, so the NEW check 5 above states the
authorization explicitly and constrains it to the single `agentic_allowed` account.
