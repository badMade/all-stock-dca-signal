# Trading Strategies

Three modular Python tools for backtesting, sentiment monitoring, and weekly strategy health checks.

---

## 1. `backtest.py` — Vectorbt Backtester

Backtest a **Moving Average Crossover** and/or **RSI Reversion** strategy on any list of tickers with realistic transaction costs and slippage.

### Install
```bash
pip install vectorbt yfinance pandas numpy pandas-ta rich
```

### Usage
```bash
# Run both strategies on default tickers (AAPL, MSFT, GOOGL, NVDA, SPY)
python backtest.py

# MA Crossover only, custom tickers and windows
python backtest.py --tickers AAPL TSLA NVDA --strategy ma_crossover --fast-window 20 --slow-window 100

# RSI Reversion with tighter costs
python backtest.py --strategy rsi_reversion --commission 0.0005 --slippage 0.0002
```

### Key flags
| Flag | Default | Description |
|---|---|---|
| `--tickers` | AAPL MSFT GOOGL NVDA SPY | Space-separated ticker list |
| `--strategy` | `both` | `ma_crossover`, `rsi_reversion`, or `both` |
| `--start` | 5 years ago | Start date (YYYY-MM-DD) |
| `--end` | today | End date (YYYY-MM-DD) |
| `--fast-window` | 50 | MA crossover fast window |
| `--slow-window` | 200 | MA crossover slow window |
| `--rsi-period` | 14 | RSI period |
| `--rsi-low` | 30 | RSI oversold threshold |
| `--rsi-high` | 70 | RSI overbought threshold |
| `--commission` | 0.001 | Per-trade commission (10 bps) |
| `--slippage` | 0.0005 | Price impact slippage (5 bps) |

### Output
- Per-ticker metrics table: Sharpe, Sortino, Max Drawdown, CAGR, Calmar, Win Rate, Total Trades
- Benchmark comparison (SPY buy-and-hold)
- Aggregated portfolio-level metrics

---

## 2. `sentiment_monitor.py` — FinBERT Sentiment Monitor

Scores recent news headlines and SEC filings for a watchlist of tickers using **ProsusAI/FinBERT**, tracks 30-day rolling sentiment, and emails a daily briefing flagging significant shifts.

### Install
```bash
pip install transformers torch requests pandas numpy jinja2
```

### Environment variables
| Variable | Required | Description |
|---|---|---|
| `NEWS_API_KEY` | Yes | [newsapi.org](https://newsapi.org) free key |
| `SMTP_USER` | Yes | Sender Gmail address |
| `SMTP_PASSWORD` | Yes | Gmail [App Password](https://myaccount.google.com/apppasswords) |
| `RECIPIENT_EMAIL` | Yes | Your email address |
| `SMTP_HOST` | No | Default: `smtp.gmail.com` |
| `SMTP_PORT` | No | Default: `587` |
| `WATCHLIST` | No | Comma-separated tickers (overrides default) |

### Usage
```bash
export NEWS_API_KEY=your_key
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=your_app_password
export RECIPIENT_EMAIL=you@gmail.com

# Live run — scores and emails
python sentiment_monitor.py

# Dry-run — prints HTML to stdout, no email sent
python sentiment_monitor.py --dry-run

# Custom tickers and z-score threshold
python sentiment_monitor.py --tickers AAPL TSLA NVDA --threshold 2.0
```

### Key flags
| Flag | Default | Description |
|---|---|---|
| `--tickers` | AAPL TSLA NVDA META AMZN | Watchlist |
| `--days` | 7 | News lookback window |
| `--threshold` | 1.5 | Z-score threshold for "significant shift" |
| `--dry-run` | False | Print email HTML, skip sending |
| `--history-file` | `./sentiment_history.json` | Path to rolling history store |

### How it works
1. Fetches last N days of headlines (NewsAPI) + last 3 SEC filings per ticker (EDGAR, free)
2. Batch-scores all texts through FinBERT in one pass
3. Computes a weighted sentiment score: `(positive − negative) × confidence`
4. Maintains a 30-day rolling history in `sentiment_history.json`
5. Flags tickers with `|z-score| ≥ threshold` as significant shifts
6. Emails an HTML briefing: top 3 movers with triggering headlines, full summary table

---

## 3. `strategy_monitor.py` — Weekly Strategy Health Monitor

Every Monday, computes Sharpe, Sortino, Max Drawdown, CAGR, Beta, Alpha, and Calmar for 5 quant strategies, appends results to a Google Sheet, and emails an alert if any strategy breaches a threshold.

### Install
```bash
pip install yfinance vectorbt pandas numpy requests scipy jinja2
```

### Environment variables
| Variable | Required | Description |
|---|---|---|
| `SMTP_USER` | Yes | Sender Gmail address |
| `SMTP_PASSWORD` | Yes | Gmail App Password |
| `RECIPIENT_EMAIL` | Yes | Your email address |
| `PIPEDREAM_TOKEN` | Yes | [Pipedream API key](https://pipedream.com/settings/account) for Sheets write |
| `SMTP_HOST` | No | Default: `smtp.gmail.com` |
| `SMTP_PORT` | No | Default: `587` |
| `GOOGLE_SHEET_ID` | No | Override the default sheet ID |
| `GOOGLE_SHEET_URL` | No | Override the default sheet URL |

### Google Sheet
Results are appended to the **[Strategy Monitor](https://docs.google.com/spreadsheets/d/1vGR8HJ4KRM6YCHj3_W38cksIryEFALSouyS-WIxrYGI/edit)** spreadsheet (`Weekly_Report` tab) each run.

### Usage
```bash
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=your_app_password
export RECIPIENT_EMAIL=you@gmail.com
export PIPEDREAM_TOKEN=your_pipedream_token

# Dry-run — prints metrics table + email HTML, no sends
python strategy_monitor.py --dry-run

# Force alert email even when all strategies are healthy
python strategy_monitor.py --force-alert

# Use a custom config JSON
python strategy_monitor.py --config my_config.json
```

### Default strategies monitored
| Strategy | Tickers | Signal |
|---|---|---|
| MA_Crossover | SPY | 50/200-day MA crossover |
| RSI_Reversion | QQQ | RSI(14), 30/70 thresholds |
| Momentum | AAPL MSFT GOOGL NVDA AMZN | 12-1 month momentum |
| Low_Volatility | XLU XLRE XLP XLV XLF | Minimum variance |
| Value_Factor | BRK-B JPM BAC WFC C | Price-to-52wk-high proxy |

### Alert thresholds (configurable in `THRESHOLDS` dict)
| Metric | Threshold |
|---|---|
| Sharpe Ratio | < 0.5 |
| Sortino Ratio | < 0.7 |
| Max Drawdown | < −15% |
| CAGR | < 5% |

---

## Scheduling (recommended)

To run `strategy_monitor.py` every Monday at 7 AM automatically, add to crontab:
```
0 7 * * 1 cd /path/to/trading-strategies && python strategy_monitor.py >> monitor.log 2>&1
```

For `sentiment_monitor.py` every weekday at 6 AM:
```
0 6 * * 1-5 cd /path/to/trading-strategies && python sentiment_monitor.py >> sentiment.log 2>&1
```
