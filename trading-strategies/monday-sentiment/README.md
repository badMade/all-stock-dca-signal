# Monday Morning Sentiment Monitor

A production-grade, single-file Python cron script that scans financial news
and SEC filings for a fixed watchlist, computes FinBERT-based sentiment
z-scores against a 30-day rolling baseline, and sends an HTML email alert
**only when at least one ticker crosses a configurable threshold** (default
`|z| ≥ 2.0`). Designed to run every Monday at 7:00 AM ET before market open.

---

## How It Differs from Generic Sentiment Scripts

| Feature | Generic scripts | This script |
|---|---|---|
| Alert trigger | Always sends email | **Only sends if |z| ≥ 2.0** |
| Z-score threshold | Often 1.5 (noisy) | **2.0 (stricter, fewer false positives)** |
| Data sources | News only | **NewsAPI + SEC EDGAR 8-K/10-K/10-Q** |
| NLP model | VADER or TextBlob | **ProsusAI/finbert (finance-domain BERT)** |
| Score granularity | Document-level | **Per-headline + per-filing snippet, then aggregated** |
| Email detail | Just z-score | **Top 3 triggering headlines with individual FinBERT scores** |
| History | None or in-memory | **Rolling 30-day JSON window, survives restarts** |
| Architecture | Class-heavy | **Single-file, no class overhead, cron-lean** |
| Dependency footprint | Often heavy | **4 packages: transformers, torch, newsapi-python, requests** |

---

## Requirements

```bash
pip install transformers torch newsapi-python requests
```

Python 3.9+ is required (uses `zoneinfo` from the standard library).
For Python 3.8, install `pip install backports.zoneinfo` and replace
`from zoneinfo import ZoneInfo` with `from backports.zoneinfo import ZoneInfo`.

The FinBERT model (`ProsusAI/finbert`, ~500 MB) is downloaded automatically
from HuggingFace on first run and cached in `~/.cache/huggingface/`.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `NEWS_API_KEY` | Optional | — | NewsAPI.org API key. Without it, the script runs on SEC filings only. Get a free key at [newsapi.org](https://newsapi.org/register). |
| `SMTP_USER` | Required for email | — | SMTP login username, e.g. `you@gmail.com` |
| `SMTP_PASSWORD` | Required for email | — | SMTP password or app-specific password |
| `RECIPIENT_EMAIL` | Required for email | — | Address to send the alert to |
| `SMTP_HOST` | Optional | `smtp.gmail.com` | SMTP server hostname |
| `SMTP_PORT` | Optional | `587` | SMTP server port (STARTTLS) |

**Gmail note:** If using Gmail, generate an [App Password](https://support.google.com/accounts/answer/185833)
(requires 2FA enabled). Use that as `SMTP_PASSWORD`, not your main account password.

Recommended: store env vars in a `.env` file and source it in your cron entry,
or use a secrets manager. Never commit credentials to version control.

---

## Cron Setup

### System clock is US Eastern Time (ET)

```cron
# Run every Monday at 7:00 AM ET
0 7 * * 1 cd /path/to/monday-sentiment && source .env && python sentiment_cron.py >> cron.log 2>&1
```

### System clock is UTC (common on cloud VMs)

```cron
# 12:00 UTC = 7:00 AM ET (UTC-5 in winter / UTC-4 EDT in summer)
# Use 11:00 UTC (0 11 * * 1) during Daylight Saving Time (March–November)
# Use 12:00 UTC (0 12 * * 1) during Standard Time (November–March)
# Or use a timezone-aware cron daemon:
0 12 * * 1 cd /path/to/monday-sentiment && source .env && python sentiment_cron.py >> cron.log 2>&1
```

**Timezone-aware cron (recommended for UTC systems):**

If your cron daemon supports the `TZ` variable (Vixie cron on Linux does):

```cron
TZ=America/New_York
0 7 * * 1 cd /path/to/monday-sentiment && source .env && python sentiment_cron.py >> cron.log 2>&1
```

### Sourcing environment variables from a file

```bash
# .env file (chmod 600 .env — keep it secret)
export NEWS_API_KEY=your_newsapi_key_here
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=your_app_password
export RECIPIENT_EMAIL=alerts@yourcompany.com
```

Then in cron:

```cron
0 7 * * 1 cd /path/to/monday-sentiment && source .env && python sentiment_cron.py >> cron.log 2>&1
```

### Verify the cron entry

```bash
crontab -l        # list current crontab
crontab -e        # edit crontab
```

---

## CLI Usage

```
python sentiment_cron.py [options]

Options:
  --tickers TICKER [TICKER ...]   Watchlist override (default: NVDA AAPL MSFT TSLA META)
  --threshold Z                   Z-score trigger threshold (default: 2.0)
  --days N                        News lookback window in days (default: 7)
  --history PATH                  Path to history JSON (default: ./sentiment_history.json)
  --dry-run                       Print HTML email to stdout; skip SMTP
```

### Examples

```bash
# Standard run — uses all env vars and defaults
python sentiment_cron.py

# Override watchlist and use a lower threshold
python sentiment_cron.py --tickers NVDA AMD INTC --threshold 1.5

# Dry-run: inspect email without sending
python sentiment_cron.py --dry-run

# Point to a shared history file
python sentiment_cron.py --history /shared/data/sentiment_history.json

# Look back 14 days instead of 7
python sentiment_cron.py --days 14
```

---

## How It Works (Pipeline Summary)

```
1. Load FinBERT (ProsusAI/finbert) via HuggingFace pipeline
2. Load rolling 30-day history from sentiment_history.json
   └── Prune entries older than 30 days

For each ticker:
3. Fetch headlines from NewsAPI (last 7 days, up to 50 per ticker)
4. Fetch 8-K / 10-K / 10-Q snippets from SEC EDGAR EFTS (last 7 days, up to 5)
5. Score every item with FinBERT:
   └── weighted_score = (P_positive − P_negative) × max(P_pos, P_neg, P_neu)
6. Ticker score = mean of all item scores
7. Append today's score to history
8. Z-score = (today − 30d_mean) / 30d_std   [skip if < 5 historical points]

9. Filter: keep tickers with |z| ≥ 2.0
10. Sort by |z| descending; take top 3

If zero tickers flagged:
   → Log "No significant sentiment shifts — skipping email" → exit 0

11. Build HTML email:
    └── Per-ticker card: badge, z-score, today/mean, top 3 triggering headlines
    └── Full watchlist summary table
12. Send via SMTP (or --dry-run print)
13. Save updated history → exit 0
```

---

## Example Email Output

When one or more tickers are flagged, the email looks like this:

```
Subject: 🔔 Pre-Market Sentiment Alert — 2026-06-29 | 2 stocks flagged

Header
──────
  Pre-Market Sentiment Alert
  Monday, June 29, 2026
  Run at 2026-06-29 07:00 ET  ·  2h 30m until market open (9:30 AM ET)
  [2 stocks flagged]

Ticker card — NVDA
──────────────────
  NVIDIA Corporation
  🟢 BULLISH SURGE
  Z-Score: +2.47   Today's Score: +0.3821   30-Day Mean: +0.1204

  TOP TRIGGERING ITEMS
  📰 NVIDIA announces record Q2 datacenter revenue, beats estimates by 23%
      Source: Reuters  ·  score: +0.512  [+89% | -4% | ~7%]

  📰 NVDA GPU demand surge driven by AI inference workloads, analysts raise targets
      Source: Bloomberg  ·  score: +0.441  [+84% | -6% | ~10%]

  📄 8-K filed by NVIDIA Corporation on 2026-06-28: [quarterly update excerpt]
      Source: SEC EDGAR (8-K)  ·  score: +0.389  [+81% | -7% | ~12%]

Ticker card — TSLA
──────────────────
  Tesla, Inc.
  🔴 BEARISH DROP
  Z-Score: -2.11   Today's Score: -0.2140   30-Day Mean: +0.0411

  TOP TRIGGERING ITEMS
  📰 Tesla misses Q2 delivery estimates; margins compress further
      Source: CNBC  ·  score: -0.394  [+5% | -87% | ~8%]
  ...

No action required: AAPL, MSFT, META are within normal range (|z| < 2.0).

Full Watchlist Summary
──────────────────────
  AAPL   Apple Inc.                +0.1021   +0.43   ✅ Normal
  META   Meta Platforms, Inc.      +0.0812   +0.71   ✅ Normal
  MSFT   Microsoft Corporation     +0.0934   -0.12   ✅ Normal
  NVDA   NVIDIA Corporation        +0.3821   +2.47   ⚠️ FLAGGED
  TSLA   Tesla, Inc.               -0.2140   -2.11   ⚠️ FLAGGED
```

When no tickers are flagged, **no email is sent** and the log records:

```
2026-06-29 07:02:14 [INFO] No significant sentiment shifts detected (threshold |z| >= 2.0) — skipping email.
```

---

## Threshold Calibration Guide

The z-score threshold is the single most important tuning parameter.

### When to use |z| ≥ 1.5

Use 1.5 when you want **maximum coverage** — you'd rather investigate a few
false positives than miss a real move. Good for:

- High-volatility tickers (meme stocks, small-cap biotech)
- Situations where you have few historical data points (< 10 days)
- Experimental runs to see what the script catches over your watchlist
- If you receive the email manually and filter it yourself

**Downside:** You'll get emails most Mondays. Alert fatigue sets in quickly.

### When to use |z| ≥ 2.0 (default — recommended)

2.0 corresponds roughly to the 95th percentile of a normal distribution.
About 1 in 20 observations should naturally cross this threshold purely by
chance — so weekly cron runs will produce an alert roughly once a month
even with flat underlying sentiment. A real news event typically pushes
scores to 2.5–3.5+, so this threshold does a good job separating signal
from noise.

Best for:
- Blue-chip / large-cap watchlists (NVDA, AAPL, MSFT, TSLA, META)
- Anyone who wants a tight inbox — only genuine regime shifts

### When to use |z| ≥ 2.5 or 3.0

Use ≥ 2.5 when you only want to hear about **extreme, rare events** —
earnings disasters, regulatory actions, product recalls, or major macro
shocks. With a 5-ticker watchlist and 2.5 threshold, you might see an alert
only once every 2–3 months.

Good for:
- Busy executives who want zero noise
- Supplementary alert layer on top of a 2.0-threshold script
- Long-duration holds where weekly noise is irrelevant

### Practical calibration workflow

1. Run with `--dry-run --threshold 1.5` for 4 weeks, logging all z-scores.
2. Inspect the `sentiment_history.json` to understand your watchlist's
   typical score distribution.
3. Choose a threshold at the 90th–97th percentile of your historical z-scores.
4. Review triggered emails for 2–3 weeks; if >60% are not actionable,
   raise threshold by 0.25.

---

## How to Extend

### Adding tickers to the default watchlist

Edit `DEFAULT_WATCHLIST` near the top of `sentiment_cron.py`:

```python
DEFAULT_WATCHLIST: List[str] = ["NVDA", "AAPL", "MSFT", "TSLA", "META", "AMZN", "GOOGL"]
```

Also add the company name to `COMPANY_NAMES` for clean email display:

```python
COMPANY_NAMES: Dict[str, str] = {
    ...
    "AMZN": "Amazon.com, Inc.",
    "GOOGL": "Alphabet Inc.",
}
```

Or pass `--tickers` at runtime without editing the file:

```bash
python sentiment_cron.py --tickers NVDA AAPL MSFT TSLA META AMZN GOOGL
```

### Adding a new data source (e.g. Reddit/StockTwits)

1. Write a new `fetch_<source>_items(ticker, ...) -> List[dict]` function
   following the same pattern as `fetch_news_headlines` and `fetch_sec_filings`.
   Each item must be created with `make_item(text, source, item_type)`.

2. In `compute_ticker_score()`, call your new function and extend the `items` list:

   ```python
   reddit_items = fetch_reddit_items(ticker, lookback_days)
   items.extend(reddit_items)
   ```

3. The rest of the pipeline (FinBERT scoring, z-score, email) is automatic —
   no other changes needed.

### Switching from Gmail to another SMTP provider

Set the env vars:

```bash
export SMTP_HOST=smtp.sendgrid.net
export SMTP_PORT=587
export SMTP_USER=apikey
export SMTP_PASSWORD=SG.your_sendgrid_api_key
```

The script uses STARTTLS on port 587 universally — compatible with most
business SMTP relays (SendGrid, Mailgun, AWS SES, Postmark).

### Running on multiple schedules

Create separate cron entries pointing to different history files:

```cron
# Monday pre-market (7 AM ET)
0 7 * * 1 cd /monday-sentiment && python sentiment_cron.py >> cron.log 2>&1

# Daily pre-market variant (same script, lower threshold, separate history)
0 7 * * 1-5 cd /monday-sentiment && python sentiment_cron.py --threshold 1.5 --history ./daily_history.json >> daily.log 2>&1
```

### Persisting history in a database

Replace `load_history` and `save_history` with functions that read/write to
SQLite or Postgres. The in-memory structure (`dict[ticker -> list[{date,score}]]`)
maps directly to a simple two-column table.

---

## File Structure

```
monday-sentiment/
├── sentiment_cron.py        # Main script — everything in one file
├── README.md                # This file
├── sentiment_history.json   # Auto-created on first run (git-ignore this)
├── sentiment_history.json.bak  # Backup of corrupted history (if any)
└── cron.log                 # Redirect stdout/stderr here in cron entry
```

Add `sentiment_history.json` and `cron.log` to `.gitignore` if you keep
this directory in version control.

---

## Error Handling Summary

| Scenario | Behavior |
|---|---|
| `NEWS_API_KEY` missing | Logs warning; continues with SEC filings only |
| NewsAPI rate limit / network error | Logs warning; that ticker scored from SEC only |
| SEC EDGAR request fails | Logs warning; that ticker scored from news only |
| No data found for a ticker | Score defaults to 0.0; included in summary table |
| FinBERT fails to load | Logs clear message + pip hint; **exits 1** (run is meaningless) |
| History file corrupted | Backs up `.bak`; starts fresh history |
| SMTP delivery fails | Logs error; **exits 0** (don't fail cron over email) |
| Missing SMTP env vars | Logs error; exits 0 cleanly |
| Unexpected per-ticker exception | Logs traceback; assigns 0.0 and continues |

---

## License

MIT — use freely, no warranty, not investment advice.
