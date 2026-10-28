path = "scripts/generate_perplexity_backup_prompts.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: UUIDv5
anchor1 = "CRITICAL: this prompt directs fully autonomous money placement. Strict rule adherence is the only safeguard. Stop the batch without action if any constraint is not explicitly met. Never modify constraints or attempt to recover from a rule breach."
replacement1 = f"{anchor1}\nYou MUST generate deterministic reference UUIDv5s using the namespace `1b671a64-40d5-491e-99b0-da01ff1f3341` and the string `<YYYY-MM-DD>_slot_<slot>_<ticker>`."
content = content.replace(anchor1, replacement1)

# Fix 2: CLAUDE_TICKERS
anchor2 = "The approved universe has @@N@@ symbols: @@UNIVERSE@@. Never place an order for a symbol not in this list."
replacement2 = f"{anchor2} Any exclusions passed from Claude (CLAUDE_TICKERS) must be dropped from the batch but do not fail the slot."
content = content.replace(anchor2, replacement2)

# Fix 3: finance NO_DATA / DISAGREEMENT
anchor3 = "Also fetch the account's available buying power in USD; never place with a missing or unreadable buying power."
replacement3 = f"{anchor3} If a finance API returns no data, mark it as `finance NO_DATA`. If multiple sources disagree significantly, mark it as `Finance DISAGREEMENT`."
content = content.replace(anchor3, replacement3)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
