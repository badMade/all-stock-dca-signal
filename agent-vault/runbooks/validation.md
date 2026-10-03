# Runbook: validation checks

Run these after setup and after any Foundation update. Commands assume the repo root.

## 1. Determinism (all-stock-dca dry run)

```bash
base="$(mktemp -d "${TMPDIR:-/tmp}/dca.XXXXXX")"; out="$base/dca-demo"
python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir "$out"
mv "$out" "$base/first"
python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir "$out"
diff -r "$base/first" "$out"
```

Both runs use the same path because a run records its own output directory; two different directory names
would differ in that one line by construction.

Pass: no output. Any other difference is a finding; note whether it is a timestamp or a run id.

## 2. Hook resolves this vault

```bash
agent-vault/scripts/vault-path.sh      # prints .../agent-vault/vault
cat ~/.config/loop-and-gate/vault      # should not exist on this machine
```

Then start `claude` inside `agent-vault/` and confirm the `=== WORKING MEMORY` block names `vault/MEMORY.md`.

## 3. Cache reuse (two sessions)

Open a session in `agent-vault/`, note the input and cached-token figures, exit, and open a second one. The second
should show cached tokens for the memory prefix. Record both readings in a Daily note; this needs your real usage
numbers.

## 4. Parallel sessions do not clobber

Run two sessions on different tasks, each in its own git worktree, let both reflect and commit, then merge and run:

```bash
git log --oneline -n 10
```

Pass: both commits are present and each touches only its own Daily and Reflections paths.

## 5. Notion isolation

Follow the isolation check in `notion-export.md`. Zero Notion writes from Claude Code is the pass condition.

## 6. Self-improvement

After three or more sessions, ask for an improvement pass. Pass: at least one lesson is judged repeated, the verifier
returns `keep`, and `git show` displays a reviewable diff. This step is judged by the agent reading the notes, so it
has no automated test.
