# Local `.env` loader for executor secrets

Status: accepted (2026-10-03). Operator approved the design and the test first.

## Context

The scheduled executors run in a sandbox whose shell does not inherit the operator's
macOS environment. On 2026-09-23 the 9:35 slot logged `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`
unset in the executor shell (Gate 7's account pin would have failed on its own) and
`DCA_ALERT_EMAIL_ADDRESS` unresolvable, so the email alert channel delivered nothing and
only a Gmail draft was saved. `launchctl setenv` and `~/.zshrc` do not reach that shell.

Every executor shell command starts fresh with no carried environment (see
`docs/live_wiring.md`), so sourcing a file in the prompt only helps the one command it
prefixes. A prompt-only fix would have to repeat the prefix on every `python3 -m scripts.*`
call across seven prompts, and the first call that forgot it would run without the account.

## Decision

Add one standard-library module, `dca_env.py`, and call it once from `dca_paths.py` at
import. `dca_paths` is the dependency-free module that `near_close_dca` (and through it
`dca_config`, `dca_runner`, and every `scripts.*` entry the prompts invoke) already
imports before anything reads the environment, including `near_close_dca`'s import-time
`ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` read. Loading there means every command sees the same
values with no prompt discipline.

The loader's contract is deliberately narrow:

* **Allowlist.** Only `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`, `DCA_ALERT_EMAIL_ADDRESS` and
  `DCA_CRON_TRACKING_ROOT` are read. Other lines are ignored and never exported.
* **Never overrides.** A variable already set and non-empty wins, so a real export (cloud
  clone, CI, a developer shell) always beats the file. Cloud-clone runs are unaffected.
* **Fail closed on a bad file.** The file must be a regular, non-symlink file owned by the
  invoking user with no group or world access (`0600`). It is opened with `O_NOFOLLOW` and
  validated with `fstat` on the same descriptor, mirroring the console's `--auth-file`
  rules. A violation raises `ValueError` naming the rule, never the contents, so a
  world-readable secret stops `import dca_paths` instead of being used.
* **Absent file is a no-op.** A checkout without `.env` behaves exactly as before.
* **Off switch.** `DCA_DISABLE_DOTENV=1` disables loading. `conftest.py` sets it so the
  suite never reads a real operator file.

Wiring: `dca_env` is listed in `pyproject.toml` `py-modules` (a wheel that ships
`dca_paths` without it would fail on import). The tracked template is `.env.example`; the
real `.env` stays gitignored.

## Consequences

* `import dca_paths` now has a side effect and can raise on an insecure `.env`. The error
  names the fix (`chmod 600 .env`). `scripts/drift_detection.py` and
  `scripts/check_live_prompt_drift.py` do not import it; neither needs these variables.
* `AGENTS.md`'s secrets policy says to use environment variables or an approved external
  secret store, and to keep `.env` contents out of git. This design complies (the file is
  gitignored and only populates environment variables), but a local plaintext file is a
  weaker store than a vault. The `0600` and ownership checks narrow that gap; they do not
  close it for an actor already running as the operator.
* The account number still appears in the executor's tool-call arguments, because the
  native `place_equity_order` tool takes the literal value. The secret rule against writing
  it to alerts, artifacts or messages is unchanged.
* The executor prompts need two small edits (a Gate 2 environment check and the Gate 7 pin
  command). They are drafted in `claude/dca-executor-prompts/dotenv-loader.draft.md` and
  not applied.

## Alternatives rejected

* **`set -a; . ./.env` in each prompt command.** Brittle: seven prompts, dozens of commands,
  and a missed prefix runs a live command without the account.
* **Resolve the account from the Robinhood MCP `get_accounts` at Gate 2** (see
  `claude/dca-executor-prompts/local-935-account-from-mcp.draft.md`). It removes the need
  for a secret file but does not cover `DCA_ALERT_EMAIL_ADDRESS`, and the order-history tool
  text tells callers never to default the account from `get_accounts`. It stays available as
  a fallback for the account number only.
* **Loading from `dca_config` as well as `dca_paths`.** Redundant: `dca_config` imports
  `near_close_dca`, which imports `dca_paths` first.
* **An arbitrary `DCA_ENV_FILE` path override.** Not needed; it would widen where a secret
  can be read from. `dca_env.REPO_ENV_PATH` stays patchable for tests.
