# Upstream provenance

This folder is a vendored working copy of the loop-and-gate Foundation.

| Field | Value |
| --- | --- |
| Project | slogsdon/loop-and-gate-foundation |
| Version | 0.5.0 |
| Commit | 2f0fe52 (2f0fe5234102cd8ae2beea1ce4fdac80c2d0721c, 2026-09-30) |
| License | MIT, kept verbatim in `LICENSE` (Shane Logsdon) |
| Why vendored | The skills must be editable so `improve` can refine them; the plugin cache is read-only |
| Dependency note | Three shell scripts and markdown, no runtime packages. Single maintainer and a new project, so the commit is pinned |

## Core scripts, pinned

These three files are core Foundation logic. `tests/test_agent_vault.py` fails if one drifts from its pin, and
`improve` must not edit them. Two are byte-identical to upstream. `session-start-hook.sh` carries one recorded
local patch: its `sha256` is the patched file and its `upstream-sha256` is the original (see below).

sha256  c05b887a428c8158ba405a9ad8173b8d95ef2c76d716534a6b2d72369266ded9  scripts/vault-path.sh
sha256  bb3527dfe56048e786e4135e7fc2e6e8783582f33f55da2a36f4e17f1bcef9c8  scripts/session-start-hook.sh
upstream-sha256  2ce182560bc5e3eef612c12928784f3ec3ebb77acfbf1b74441995a381bb019b  scripts/session-start-hook.sh
sha256  96ff8b6df3440a8bb22e675ea8d05d8188f2f041c1ce11bb20ff8e89b8d3a6f1  scripts/inject-operating-rules.sh

## Changes from upstream

| File | Change |
| --- | --- |
| `CLAUDE.md` | Two stale sentences about a gitignored vault fixed; "This copy inside all-stock-dca" section added before "Hard rules". The Hard rules section is untouched |
| `config.yaml` | Task keys added below the original `vault:` line |
| `.claude/settings.json` | `permissions.deny` added (Notion writes, broker order tools); hooks as upstream |
| `scripts/session-start-hook.sh` | Patched after PR review (four defects, each with a regression test, plus its no-vault message): picks the newest note that has a session instead of the empty scaffold it just created, so the first session of a day still sees yesterday's Open items; bounds the injected reflection (tail kept, pointer to the full file); lowers the cap from 9500 to 8500 for the extra markers. Creates `Daily/` and `Reflections/` with `mkdir -p` (the original aborted with no memory block when one was missing). To be reported upstream. Re-vendor with care: re-apply or drop the patch |
| `skills/loop/SKILL.md` | Fail branch restores from a start snapshot (`git stash create`, plus a copy of files already untracked) instead of the index, and marks files a failed worker newly created instead of leaving them live. Skill ends a few lines past the soft 80-line cap |
| `skills/setup/`, `scripts/setup.sh` | Not vendored: `setup.sh` records `~/.config/loop-and-gate/vault`, which `vault-path.sh` ranks above the tracked vault, so one stray run would move every later session's memory out of the repo. The vault is already scaffolded. `CLAUDE.md`, `README.md` and the hook's no-vault message no longer point at it |
| `skills/add-kits/` | Not vendored: it installs sibling kits, which is a new dependency needing sign-off |
| `skills/` | Three overlays added: `execute-dca`, `analyze-capex`, `backtest-dltp` |
| `vault/` | Ours. Upstream ships `vault.example/` only |

## Updating

1. Read the upstream changelog and the diff of the three pinned scripts first.
2. Re-vendor from a clean checkout of the new commit, re-apply the changes above, and update this file.
3. Re-pin the hashes, run `tests/test_agent_vault.py`, and review the diff before merging.
