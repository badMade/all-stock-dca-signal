# Runbook: weekly Notion export (manual)

The agent never writes to Notion; the write tools are denied in `.claude/settings.json`. This is a human ritual, on
purpose: a person reads every export, the narrative stays clean, and there is nothing to collide.

1. Open `agent-vault/vault/Daily/` in Obsidian (or any editor).
2. Select last week's notes.
3. Copy the findings into the Notion "Weekly Syncs" database by hand, rewriting for a reader who was not there.
4. Async alternative: export the week's Daily notes as markdown and attach them to a "Daily Exports" database.
   Still manual, still weekly.
5. Skim `Reflections/` for the same week and copy over any lesson worth a human decision.

Isolation check, any time: ask a session to create a Notion page. It should be refused by the permission rule. If it
succeeds, the deny list is broken; fix `.claude/settings.json` before anything else.
