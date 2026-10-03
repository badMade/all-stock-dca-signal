# agent-vault

A self-improving memory vault for the recurring all-stock-dca, capex and dl-trading-pipeline work. It is the
loop-and-gate Foundation (vendored, pinned, see `UPSTREAM.md`) plus three task skills.

## Start a session

```bash
cd agent-vault
claude
```

Only a session started in this folder loads the vault. A session at the repo root does not, on purpose: ordinary
code sessions never pay for the memory block or write into it. Upstream's first-run setup is not vendored here, and the loop-and-gate
plugin must not be enabled for this project: its setup would make a machine-local vault outrank this one.

## Layout

| Path | What it is |
| --- | --- |
| `vault/MEMORY.md` | The index. Short; written once, at session close |
| `vault/Daily/`, `vault/Reflections/` | One note per day, named `YYYY-MM-DD.md` only |
| `vault/Knowledge/` | One note per topic: the three task rule notes and `threshold-review.md` |
| `vault/Profiles/`, `vault/Inbox/` | Voice and taste seeds; raw captures awaiting triage |
| `skills/` | Foundation skills plus `execute-dca`, `analyze-capex`, `backtest-dltp`. `.claude/skills` links here |
| `runbooks/` | Step-by-step walkthroughs for each recurring task and the validation checks |
| `config.yaml` | Vault pointer and task thresholds. DCA limits point at the repo's own safeguards |
| `.claude/settings.json` | The SessionStart hooks, and denied Notion write and broker order tools |

## Boundaries

- This vault holds task rules, session logs and reflections. Notion holds human-curated reference and is read-only
  for the agent. The weekly export to Notion is manual (`runbooks/notion-export.md`).
- The strategy library stays dry-run only. `execute-dca` runs `python run_dca.py --demo` and never places orders.

## Status

| Task | Skill | State |
| --- | --- | --- |
| all-stock-dca | `execute-dca` | Dry-run path usable |
| capex monitoring | `analyze-capex` | Untested draft; no watched companies yet |
| dl-trading-pipeline | `backtest-dltp` | Untested draft |

Thresholds other than the DCA limits are `UNVERIFIED`; see `vault/Knowledge/threshold-review.md`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No `=== WORKING MEMORY` block at session start | Were you in `agent-vault/`? Run `scripts/vault-path.sh`; it should print `.../agent-vault/vault` |
| It prints another path | `cat ~/.config/loop-and-gate/vault`. That file outranks this vault; remove it if it is not wanted |
| "Latest note" is wrong | A Daily or Reflections file is not named `YYYY-MM-DD.md` |
| Hook did not run | `scripts/*.sh` lost the exec bit (`chmod +x`), or `.claude/` was gitignored |
| An empty untracked `vault/Daily/YYYY-MM-DD.md` shows in `git status` | Normal: the hook creates it at session start. Stage notes by path, never `git add -A` |
| A pinned script test fails | A core script drifted from upstream; restore it, do not re-pin |

## Contributing

Run `ruff check .` and `python -m pytest tests/test_agent_vault.py` from the repo root. Change core Foundation
scripts only by re-vendoring (`UPSTREAM.md`). Skill edits come from `improve` passes or deliberate PRs.
