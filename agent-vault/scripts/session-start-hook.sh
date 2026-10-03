#!/usr/bin/env bash
# SessionStart hook: injects working memory into every Claude Code session
# in this repo. Memory loading as infrastructure — it can't be forgotten,
# unlike a skill the model must remember to invoke.
# stdout becomes session context.
set -euo pipefail

cd "$(dirname "$0")/.."

# Resolve the vault through the shared resolver (scripts/vault-path.sh) so the
# hook and the skills always agree on ONE base path. It returns the recorded
# setup path, or a clone's ./vault, and deliberately never the plugin cache's
# bundled vault — a plugin install must go through setup.
VAULT="$("$(dirname "$0")/vault-path.sh")"

# No vault resolved. Don't hard-fail on the missing memory files below — tell the user how to restore the vault
# and let the session start clean. LOCAL PATCH (see UPSTREAM.md): upstream pointed at a first-run installer
# that this vendored copy does not ship.
if [ -z "$VAULT" ] || [ ! -f "$VAULT/MEMORY.md" ]; then
  echo "=== Loop & Gate Foundation: no vault found ==="
  echo "vault/MEMORY.md is missing. Restore the tracked vault from git, from the agent-vault folder:"
  echo "  git checkout -- vault"
  echo "Then start a new session — memory will load here automatically."
  exit 0
fi

# LOCAL PATCH (see UPSTREAM.md): under `set -euo pipefail` an `ls` of a missing folder aborted the hook (exit 2)
# before it printed anything, so the session silently started without memory.
mkdir -p "$VAULT/Daily" "$VAULT/Reflections"

today=$(date +%Y-%m-%d)
daily="$VAULT/Daily/$today.md"

# Ensure today's daily note exists so reflect has somewhere to log
if [ ! -f "$daily" ]; then
  printf '# %s\n\n## Sessions\n' "$today" > "$daily"
fi

# MEMORY.md goes first among the memory sections ON PURPOSE: kept stable
# (read-only mid-session), it anchors the mutable part of the prompt-cache
# prefix. The inject-operating-rules.sh hook runs before this one, so the even
# more stable CLAUDE.md sits above it — the two form the stable prefix together.
# Don't reorder these sections.
# 10K guard: this whole block is ONE stdout string, capped at 10,000 chars —
# over that, Claude Code offloads it to a file and only a preview reaches
# context, silently truncating MEMORY.md (the index, printed first). MEMORY.md
# and the reflection are bounded and must survive whole; the daily note grows
# unbounded via /log, so it's the one we truncate — to its TAIL, since the most
# recent sessions matter most. A visible marker tells the running agent its
# memory is partial and where to read the rest. Mirrors the guard in
# inject-operating-rules.sh, but truncates instead of only warning: the daily
# note is breached by normal /log use, with no author watching hook stderr.
# ponytail: tail -c counts bytes, so a cut mid-emoji leaves cosmetic garbage at
# the top of the kept tail; line-aware tail if that ever matters.
# LOCAL PATCH (see UPSTREAM.md): 9500 -> 8500. Up to three headers and two markers can now appear, each carrying
# the absolute vault path, so ~500 chars of chrome was not enough.
CAP=8500  # ~1,500 under the real 10,000 for section chrome + the marker lines
mem_sz=$(wc -c < "$VAULT/MEMORY.md" | tr -d ' ')

latest_reflection=$(ls -1 "$VAULT/Reflections/" 2>/dev/null | sort | tail -1)
refl_sz=0
[ -n "$latest_reflection" ] && refl_sz=$(wc -c < "$VAULT/Reflections/$latest_reflection" | tr -d ' ')
# LOCAL PATCH (see UPSTREAM.md): the reflection is bounded too. reflect appends every session of a day to the same
# file, so an unbounded `cat` can push the whole block past the cap, and Claude Code then keeps only a preview of it
# (truncating MEMORY.md, which is printed first). Give it at most half of what MEMORY.md leaves over, keep its tail
# (the most recent sessions), and let the daily note have the rest.
refl_budget=$(( (CAP - mem_sz) / 2 ))
[ "$refl_budget" -ge 0 ] || refl_budget=0
refl_shown=$refl_sz
if [ "$refl_sz" -gt "$refl_budget" ]; then
  refl_shown=$refl_budget
  [ "$refl_budget" -ge 300 ] || refl_shown=0
fi
daily_budget=$(( CAP - mem_sz - refl_shown ))

echo "=== WORKING MEMORY (injected by SessionStart hook) ==="
echo ""
echo "--- $VAULT/MEMORY.md ---"
cat "$VAULT/MEMORY.md"
echo ""

# LOCAL PATCH (see UPSTREAM.md): prefer the newest note that has a session in it. The empty scaffold created above
# sorts last on the first session of a day, so a plain `sort | tail -1` injected it and dropped yesterday's Open items.
latest_daily=""
while IFS= read -r name; do
  if grep -q '^### ' "$VAULT/Daily/$name"; then latest_daily="$name"; break; fi
done < <(ls -1 "$VAULT/Daily/" 2>/dev/null | sort -r)
[ -n "$latest_daily" ] || latest_daily=$(ls -1 "$VAULT/Daily/" 2>/dev/null | sort | tail -1)
if [ -n "$latest_daily" ]; then
  dpath="$VAULT/Daily/$latest_daily"
  dsz=$(wc -c < "$dpath" | tr -d ' ')
  echo "--- latest daily note: $dpath ---"
  if [ "$dsz" -le "$daily_budget" ]; then
    cat "$dpath"
  elif [ "$daily_budget" -lt 300 ]; then
    echo "[daily note omitted — MEMORY.md + reflection already near the 10K cap; read it at $dpath]"
    echo "loop-and-gate: memory block near 10K cap; daily note $latest_daily omitted." >&2
  else
    echo "[daily note truncated to last ${daily_budget} chars to fit the 10K SessionStart cap — full note at $dpath]"
    tail -c "$daily_budget" "$dpath"
    echo "loop-and-gate: daily note $latest_daily (${dsz}b) truncated to ${daily_budget}b to fit the 10K memory cap." >&2
  fi
  echo ""
fi

if [ -n "$latest_reflection" ]; then
  rpath="$VAULT/Reflections/$latest_reflection"
  echo "--- latest reflection: $rpath ---"
  if [ "$refl_sz" -le "$refl_budget" ]; then
    cat "$rpath"
  elif [ "$refl_shown" -eq 0 ]; then
    echo "[reflection omitted — MEMORY.md already near the 10K cap; read it at $rpath]"
    echo "loop-and-gate: reflection $latest_reflection omitted to fit the 10K memory cap." >&2
  else
    echo "[reflection truncated to last ${refl_shown} chars to fit the 10K SessionStart cap — full note at $rpath]"
    tail -c "$refl_shown" "$rpath"
    echo "loop-and-gate: reflection $latest_reflection (${refl_sz}b) truncated to ${refl_shown}b to fit the 10K memory cap." >&2
  fi
  echo ""
fi

echo "=== END WORKING MEMORY ==="
echo "Memory is loaded. Apply the latest reflection's lesson this session."
echo "Still use the session-start skill's step 6: state the goal and your assumptions before working."
