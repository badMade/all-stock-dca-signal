# The local `.env` file

The scheduled executors run in a sandbox whose shell does not inherit your Mac's
environment, so `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` and `DCA_ALERT_EMAIL_ADDRESS` arrive
unset (the 2026-09-23 failure). A gitignored `.env` in the repo root fixes that: `dca_env.py`
loads it whenever `dca_paths` is imported, which every script the prompts run does. Why this
design, and what it rejected: [adr-dotenv-loader.md](adr-dotenv-loader.md).

## Set it up

```bash
cd /path/to/all-stock-dca
cp .env.example .env
chmod 600 .env            # required; the loader refuses a group/world-readable file
$EDITOR .env              # fill in the values; never paste them into chat, an alert or an artifact
```

| Variable | Needed for |
| --- | --- |
| `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` | Live runs: Gate 4/6 order history, buying power, and the Gate 7 account pin |
| `DCA_ALERT_EMAIL_ADDRESS` | The Gate 9 email channel (`config/notifications.json`) |
| `DCA_CRON_TRACKING_ROOT` | Optional; relocates the artifact tree (default `<checkout>/cron_tracking`) |

Only those three names are read; other lines in the file are ignored. Put comments on their
own line, not after a value. A variable that is already exported and non-empty is never
overridden, so cloud clones and CI behave as before.

## Verify

From the repo root, in the shell the executor will use:

```bash
python3 -c "import dca_paths, os; print({k: bool(os.environ.get(k)) for k in ('ROBINHOOD_AGENTIC_ACCOUNT_NUMBER', 'DCA_ALERT_EMAIL_ADDRESS')})"
```

Both should print `True`. The command prints booleans only, never the values.

## Event-triggered backup settings (2026-10-31)

`scripts/backup_dispatch.py` reads these from the same `.env`; it never prints any of them:

| Name | Meaning |
| --- | --- |
| `DCA_PERPLEXITY_SENDER` | The address tied to your Perplexity account; Perplexity only accepts tasks from it |
| `DCA_SMTP_HOST`, `DCA_SMTP_USER` | Your mail provider's SMTP server (port 465, SSL) and login |
| `DCA_SMTP_PASSWORD` | A mail app password, not your account password |
| `DCA_FILLED_PROMPTS_DIR` | The private folder `scripts/fill_perplexity_backup_prompts.py --out` wrote; optional |
| `DCA_PPLX_STATE_FOLDERS` | Only when `DCA_FILLED_PROMPTS_DIR` is unset: `task=automation-uuid` pairs, comma-separated |

The GitHub watcher reads the same names from repository secrets, plus `DCA_ALERT_EMAIL_ADDRESS` and
`ROBINHOOD_AGENTIC_ACCOUNT_LAST4` to fill the prompt.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ValueError: .env: must not be group/world accessible; run: chmod 600 .env` | Editors create `0644` files. Run `chmod 600 .env`. |
| `ValueError: ... symlinks are refused` / `must be owned by the invoking user` | `.env` must be a regular file you own, not a link or a file copied in by another user. |
| A variable prints `False` | The line is missing, blank, misspelled, or has a trailing `# comment` on the value line. Blank values are skipped. |
| `.env` exists but is ignored | `DCA_DISABLE_DOTENV` is set in that shell (tests set it on purpose). |
| Works on your Mac, not in the executor | The executor mounts the repo folder; confirm `ls -la .env` shows the file from inside that shell, and check its mode. |

## Tests

`DCA_DISABLE_DOTENV=1` is set by `conftest.py`, so the suite never reads a real `.env`. The
loader's own tests are `tests/test_dca_env.py`.

## Executor prompts

The prompts reference this file in two places (a Gate 2 environment check, and the Gate 7
account pin). The edits are drafted, not applied, in
[`claude/dca-executor-prompts/dotenv-loader.draft.md`](../claude/dca-executor-prompts/dotenv-loader.draft.md).
