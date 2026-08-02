# Notion Dashboard Template

A page template that mirrors this repo's docs structure, plus conceptual
instructions for syncing between GitHub and Notion via the Notion API.

**No credentials here.** The token is supplied at runtime via an environment
variable / secret manager, never committed.

Live dashboard: `https://app.notion.com/p/391202e5e677812f9ce4c9bc0904d412`

## Page structure

```
Graded DCA Dashboard
├── Latest Run            (compact; synced daily by task 67aa802b)
│   ├── Run date (ET)
│   ├── Orders placed (ticker, drop %, multiplier, $ amount, fill)
│   ├── Spent today / week, reserve balance
│   └── Status + any errors
├── Strategy              (mirrors docs/strategy.md)
│   ├── Active universe (52 tickers)
│   ├── Sizing ladder (0.1%–8%+, max 5x)  ← older 0.1%–12% ladders are superseded
│   └── Budget & reserve rules
├── Safeguards            (mirrors docs/safeguards.md)
├── Schedule Manifest     (mirrors docs/schedule_manifest.md)
├── Performance Dashboard (full refresh weekly Fri / monthly)
│   ├── Cumulative capital deployed
│   ├── Per-ticker avg entry & shares
│   └── Slippage summary
└── Cost Tracking         (mirrors docs/cost_tracking.md; usage proxies)
```

## Conceptual GitHub ↔ Notion sync

The intent is one-directional: **repo docs/config are the source of truth**, and
a sync job pushes summarized content into Notion. Do **not** implement live API
calls with credentials in this repo — the steps below are the recipe.

1. Create a Notion internal integration; store its token as `NOTION_TOKEN` in a
   secret manager / environment variable (never in git).
2. Share the target Notion page with the integration.
3. A sync job reads the repo's `config/*.json` and `docs/*.md`, builds a Notion
   block payload, and calls the Notion API:
   - `PATCH /v1/blocks/{block_id}/children` to update a section's children, or
   - `PATCH /v1/pages/{page_id}` for page-level properties.
   Auth header: `Authorization: Bearer $NOTION_TOKEN`, `Notion-Version: 2022-06-28`.

### Example payload (illustrative — no real token)

```json
{
  "children": [
    {
      "object": "block",
      "type": "heading_2",
      "heading_2": { "rich_text": [{ "type": "text", "text": { "content": "Latest Run — 2026-07-06" } }] }
    },
    {
      "object": "block",
      "type": "paragraph",
      "paragraph": { "rich_text": [{ "type": "text", "text": { "content": "Orders: 13 · Spent today: $17.00 · Reserve: $0.00 · Status: success" } }] }
    }
  ]
}
```

```bash
# Illustrative only. NOTION_TOKEN comes from the environment, never from git.
curl -s -X PATCH "https://api.notion.com/v1/blocks/${LATEST_RUN_BLOCK_ID}/children" \
  -H "Authorization: Bearer ${NOTION_TOKEN}" \
  -H "Notion-Version: 2022-06-28" \
  -H "Content-Type: application/json" \
  --data @latest_run_payload.json
```
