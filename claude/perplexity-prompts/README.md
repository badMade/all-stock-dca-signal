# Perplexity backup-task prompts (corrected text)

Six files, one per Perplexity backup task: 9:55, 10:50 (watchdog that delegates
to the Mac Studio), 11:50, 12:50, 1:50/2:50 (one task, two fires) and 3:45.

**These do not run from here.** Perplexity cannot read this repo, so a human
pastes each file into the matching task. Editing a file changes nothing live,
exactly like `../dca-executor-prompts/live-mirror/`.

## What they fix (2026-10-06)

The live prompts rejected any ticker bought earlier in the day ("never
duplicate a ticker...", "no duplicate same-day order", "no earlier same-day buy
for that ticker by any runner") and had no top-up guard, so a cheaper repeat
could never be bought. Each file now:

* replaces that rule with the same-day top-up guard, copied verbatim from
  `../PERPLEXITY-SAME-DAY-TOPUP-GUARD.md` section 2 (lower price only, sized
  `desired - prior`, sub-$1 difference held, fail closed on unreadable history);
* keeps only the two real exclusions: same slot window as Claude, and one order
  per ticker per run;
* stands down only when the primary is still placing orders (the in-window buy set grew
  between the two polls); otherwise Claude's tickers are excluded one by one and the rest
  are bought; placements re-derive tier and size from a fresh quote and use a deterministic
  (UUIDv5) `ref_id`; finance NO_DATA never blocks but finance DISAGREEMENT does;
* adds the secrets / prompt-injection / fail-closed section the Claude prompts
  already have.

## Secrets

This repo is public. The account number and operator email are placeholders
(`${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}`, `${DCA_ALERT_EMAIL_ADDRESS}`) and are
substituted only inside the Perplexity task at paste time. That substitution is
the only permitted divergence from the live text.

## Keeping them honest

`tests/test_perplexity_prompt_mirror.py` fails if the old rule returns, if the
guard block drifts from the guard doc, or if an account-shaped number or email
appears. To change the guard: edit the doc, re-run the generator logic (or edit
each mirror identically), then re-paste into Perplexity.

Not covered by tests, because this repo cannot see it: whether the live Perplexity
task text still matches. After pasting, the first run's report should show the
new outcome labels ("price not lower", "sub-$1 difference outstanding", ...) and
never "Duplicate gate (already bought today)".
