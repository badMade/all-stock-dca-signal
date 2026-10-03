# DRAFT prompt edits: use the local `.env` loader (all seven live executor prompts)

Not applied anywhere. These are edits to the committed `live-mirror/` bodies, which mirror the
live tasks at `~/Claude/Scheduled/<taskId>/SKILL.md`. A human applies them through
`update_scheduled_task`'s full-body replacement, then re-captures the mirrors (see this
directory's README). Run `python3 -m scripts.check_live_prompt_drift` afterwards.

Prerequisite: the `dca_env` loader is merged and the operator has created `.env`
(`docs/local_env_file.md`, `docs/adr-dotenv-loader.md`). Until then, do not apply these: the
new Gate 2 check would abort every fire.

Why: the executor sandbox does not inherit the operator's environment, so on 2026-09-23 the
account number and alert address were unset. Importing `dca_paths` now loads `.env`, so every
`python3 -m scripts.*` command sees the values with no per-command prefix. Shell state does
not persist between calls, which is why a per-command export was rejected.

## Edit 1 -- Gate 2: add an ENVIRONMENT CHECK after the single checkout is chosen

Insert immediately before "If none of these yields a directory containing ...":

> ENVIRONMENT CHECK. Once exactly one checkout survives, from its root run:
> `python3 -c "import dca_paths, os; print({k: bool(os.environ.get(k)) for k in ('ROBINHOOD_AGENTIC_ACCOUNT_NUMBER', 'DCA_ALERT_EMAIL_ADDRESS')})"`.
> Importing `dca_paths` loads the operator's gitignored `.env` (`dca_env`; `docs/local_env_file.md`), so
> this works even though the executor shell does not inherit the operator's exports. Print booleans
> only, never a value (SECRET RULE).
> * The import raises `ValueError` (insecure `.env` permissions, a symlink, wrong owner): abort
>   `Failing gate: Gate 2` with "local .env rejected: <the error text>". The text names a rule, never
>   contents, so it is safe to quote. Place nothing.
> * `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` is False: abort `Failing gate: Gate 2` with "account number
>   environment variable not visible to the executor shell; create .env per docs/local_env_file.md".
>   Place nothing. This is the same condition Gate 7's pin would otherwise hit minutes later.
> * `DCA_ALERT_EMAIL_ADDRESS` is False: do NOT abort. Record "email alert channel has no recipient" for
>   Gate 9's report and in the first alert you send, because channel 3 cannot deliver. Channels 1 and 2
>   still run.
> A closed-day END_SILENTLY at Gate 0 never reaches this check and is unaffected.

## Edit 2 -- Gate 7: replace the resolve instruction in "RESOLVE AND PIN THE ACCOUNT"

OLD:
> Resolve it to its actual environment value through the shell that will construct the call, and fail closed ...

NEW:
> Resolve it by running, from the repo root, `python3 -c "import dca_paths, os; print(os.environ.get('ROBINHOOD_AGENTIC_ACCOUNT_NUMBER', ''))"`
> (the import loads the gitignored `.env`; see Gate 2's ENVIRONMENT CHECK). Use that printed value as the account for every
> native call, and fail closed ...

Keep the rest of the paragraph unchanged (unset, empty, unexpanded placeholder, wrong shape, one value
everywhere). The value appears only in tool arguments. It still never goes into an alert, artifact or
message-center post.

## Edit 3 -- SECRET RULE: one added sentence

Append to the TRUNCATION-SAFE SECRET RULE:

> The account number and alert address come from the operator's local `.env` via `dca_paths`; the file's
> contents are never read, printed or quoted, and Gate 2 reports only whether each variable is set.

## No other edits

Gates 4, 5, 6, 6.5, 6.7 and 9 run only `python3 -m scripts.*` commands that import `dca_paths`
(verified for `generate_market_open_candidates`, `prior_reference_bindings`, `same_day_top_up_gate`,
`assemble_order_pages`, `finalize_run_evidence`). `scripts/drift_detection.py` does not import it and
needs neither variable. Email delivery needs no prompt change: `config/notifications.json` already
references `${DCA_ALERT_EMAIL_ADDRESS}` and `dca_config.expand_config_env` resolves it from the
environment.

## Test before enabling

1. On the operator's Mac: `cp .env.example .env`, `chmod 600 .env`, fill both values.
2. In a scheduled-task test run (outside 9:30 to 10:00 ET it stops at Gate 1, so run the Gate 2 sequence
   by hand), confirm the ENVIRONMENT CHECK prints `True` for both.
3. Confirm `chmod 644 .env` makes the check abort with the `chmod 600` message, then restore `0600`.

## Fallback if the sandbox cannot see the file

If the check prints `False` while `.env` exists on the Mac, the executor's mount is not exposing it.
Use `local-935-account-from-mcp.draft.md` for the account number (it does not cover the alert address)
and fix the mount before relying on this loader.
