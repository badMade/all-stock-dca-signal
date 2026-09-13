"""
Monday Strategy Monitor
=======================
Weekly quantitative strategy performance monitoring with HTML email alerting
and CSV report export.

Requirements:
    pip install yfinance alpaca-py>=0.8.0 vectorbt pandas numpy requests scipy jinja2

Usage:
    python task3_monitor.py                          # normal run
    python task3_monitor.py --dry-run                # print output, no writes/sends
    python task3_monitor.py --force-alert            # send email even if no breaches
    python task3_monitor.py --config path/to/cfg.json  # override defaults

Environment variables:
    SMTP_HOST               SMTP server hostname          (default: smtp.gmail.com)
    SMTP_PORT               SMTP server port              (default: 587)
    SMTP_USER               Sender email address          (required for send)
    SMTP_PASSWORD           Sender email password/token   (required for send)
    RECIPIENT_EMAIL         Destination email address     (required for send)
    PIPEDREAM_TOKEN         Pipedream API token           (required for Sheets write)
    GOOGLE_SHEET_ID         Override the default Sheet ID (optional)
    GOOGLE_SHEET_URL        Override the default Sheet URL (optional)
"""

# Alpaca credentials — set for real-time-ready data ingestion
# export ALPACA_API_KEY=your_key
# export ALPACA_SECRET_KEY=your_secret
# Falls back to yfinance if not set.

# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------
import argparse
import csv
import json
import logging
import os
import os as _os
import smtplib
import ssl
import sys
import traceback
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import numpy as np
import pandas as pd

try:
    import vectorbt as vbt  # type: ignore
    import yfinance as yf  # type: ignore
    from alpaca.data.historical import StockHistoricalDataClient  # type: ignore
    from alpaca.data.requests import StockBarsRequest  # type: ignore
    from alpaca.data.timeframe import TimeFrame  # type: ignore

    # Jinja2 – HTML email templating
    from jinja2 import BaseLoader, Environment  # type: ignore

    # scipy – beta/alpha linear regression
    from scipy import stats  # type: ignore
except ImportError:
    vbt = None  # type: ignore
    yf = None  # type: ignore
    StockHistoricalDataClient = None  # type: ignore
    StockBarsRequest = None  # type: ignore
    TimeFrame = None  # type: ignore
    BaseLoader = None  # type: ignore
    Environment = None  # type: ignore
    stats = None  # type: ignore

_ALPACA_KEY    = _os.environ.get("ALPACA_API_KEY", "")
_ALPACA_SECRET = _os.environ.get("ALPACA_SECRET_KEY", "")
_USE_ALPACA    = bool(_ALPACA_KEY and _ALPACA_SECRET)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("StrategyMonitor")

# ---------------------------------------------------------------------------
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                        GLOBAL CONFIGURATION                             ║
# ║  Edit these values directly, or override at runtime via --config JSON   ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# ---------------------------------------------------------------------------

CONFIG: dict[str, Any] = {
    # ── Performance thresholds ─────────────────────────────────────────────
    # A strategy is flagged (ALERT) if ANY metric falls below its threshold.
    # max_drawdown threshold is negative (e.g. -0.15 means flag if DD < -15%).
    "THRESHOLDS": {
        "sharpe":       0.5,    # annualised Sharpe ratio minimum
        "sortino":      0.7,    # annualised Sortino ratio minimum
        "max_drawdown": -0.15,  # most negative acceptable drawdown
        "cagr":         0.05,   # 1-year CAGR minimum
    },

    # ── Alerting behaviour ─────────────────────────────────────────────────
    # EMAIL_ON_ALL=True  → send email even when all strategies are healthy
    # EMAIL_ON_ALL=False → only send when at least 1 strategy is flagged
    "EMAIL_ON_ALL": False,

    # ── Backtest parameters ────────────────────────────────────────────────
    "LOOKBACK_YEARS": 1,    # years of daily OHLCV to fetch
    "TRADING_DAYS":   252,  # annualisation factor
    "RISK_FREE_RATE": 0.05, # annual risk-free rate for Sharpe/Sortino
    "FEES":           0.001,# one-way transaction cost fraction

    # ── CSV export ─────────────────────────────────────────────────────────
    # Output directory for the per-run CSV report (uses current directory by default)
    "CSV_OUTPUT_DIR": ".",

    # ── SMTP / email ───────────────────────────────────────────────────────
    "SMTP_HOST":       os.environ.get("SMTP_HOST",       "smtp.gmail.com"),
    "SMTP_PORT":       int(os.environ.get("SMTP_PORT",   "587")),
    "SMTP_USER":       os.environ.get("SMTP_USER",       ""),
    "SMTP_PASSWORD":   os.environ.get("SMTP_PASSWORD",   ""),
    "RECIPIENT_EMAIL": os.environ.get("RECIPIENT_EMAIL", ""),

    # ── Optional Google Sheet link (shown as button in email) ──────────────
    # Set the GOOGLE_SHEET_URL env var if you maintain a separate sheet.
    # Pre-configured Strategy Monitor spreadsheet (created via Perplexity connector)
    # Requires PIPEDREAM_TOKEN env var at runtime to write rows.
    "GOOGLE_SHEET_ID":   os.environ.get("GOOGLE_SHEET_ID",  "1vGR8HJ4KRM6YCHj3_W38cksIryEFALSouyS-WIxrYGI"),
    "GOOGLE_SHEET_URL":  os.environ.get("GOOGLE_SHEET_URL", "https://docs.google.com/spreadsheets/d/1vGR8HJ4KRM6YCHj3_W38cksIryEFALSouyS-WIxrYGI/edit"),
    "GOOGLE_SHEET_NAME": "Weekly_Report",
    "PIPEDREAM_TOKEN":   os.environ.get("PIPEDREAM_TOKEN", ""),

    # ── Default strategies ─────────────────────────────────────────────────
    # Each strategy dict keys:
    #   name    – display name
    #   tickers – list of ticker symbols
    #   signal  – identifier used by StrategyRunner to pick logic
    #   params  – strategy-specific hyper-parameters
    "STRATEGIES": [
        {
            "name":   "MA_Crossover",
            "tickers": ["SPY"],
            "signal": "ma_crossover",
            "params": {"fast": 50, "slow": 200},
        },
        {
            "name":   "RSI_Reversion",
            "tickers": ["QQQ"],
            "signal": "rsi_reversion",
            "params": {"period": 14, "low": 30, "high": 70},
        },
        {
            "name":   "Momentum",
            "tickers": ["AAPL", "MSFT", "GOOGL", "NVDA", "AMZN"],
            "signal": "momentum",
            "params": {"skip_months": 1, "lookback_months": 12},
        },
        {
            "name":   "Low_Volatility",
            "tickers": ["XLU", "XLRE", "XLP", "XLV", "XLF"],
            "signal": "low_volatility",
            "params": {},
        },
        {
            "name":   "Value_Factor",
            "tickers": ["BRK-B", "JPM", "BAC", "WFC", "C"],
            "signal": "value_factor",
            "params": {},
        },
    ],
}

# Columns written to the CSV report and displayed in the email metrics table
_REPORT_COLUMNS = [
    "Date", "Strategy", "Sharpe", "Sortino", "MaxDD",
    "CAGR", "Beta", "Alpha", "Calmar", "Status",
]

# ---------------------------------------------------------------------------
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                       HTML EMAIL TEMPLATE                               ║
# ║  Jinja2 template defined as a module-level string constant.             ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# ---------------------------------------------------------------------------

_EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Strategy Monitor Report</title>
  <style>
    /* ── Reset & base ───────────────────────────────────── */
    body {
      margin: 0; padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   Helvetica, Arial, sans-serif;
      background: #f4f6f9;
      color: #1a1a2e;
    }
    .wrapper {
      max-width: 780px;
      margin: 32px auto;
      background: #ffffff;
      border-radius: 10px;
      overflow: hidden;
      box-shadow: 0 2px 12px rgba(0,0,0,.10);
    }

    /* ── Header banner ──────────────────────────────────── */
    .header {
      background: linear-gradient(135deg, #1a1a2e 0%, #16213e 60%, #0f3460 100%);
      padding: 28px 36px;
      color: #ffffff;
    }
    .header h1 {
      margin: 0 0 4px;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: .3px;
    }
    .header p  { margin: 0; font-size: 13px; opacity: .75; }

    /* ── Summary pills ──────────────────────────────────── */
    .summary {
      display: flex;
      gap: 16px;
      padding: 24px 36px 0;
    }
    .pill {
      border-radius: 50px;
      padding: 8px 22px;
      font-size: 14px;
      font-weight: 600;
    }
    .pill-alert { background: #fde8e8; color: #b91c1c; }
    .pill-ok    { background: #dcfce7; color: #15803d; }

    /* ── Section heading ────────────────────────────────── */
    .section-title {
      font-size: 15px;
      font-weight: 700;
      color: #374151;
      padding: 24px 36px 8px;
      border-bottom: 1px solid #e5e7eb;
      margin: 0;
    }

    /* ── Alert cards ────────────────────────────────────── */
    .alert-card {
      margin: 16px 36px;
      border: 1px solid #fca5a5;
      border-left: 5px solid #ef4444;
      border-radius: 8px;
      padding: 18px 22px;
      background: #fff8f8;
    }
    .alert-card h3 {
      margin: 0 0 12px;
      font-size: 16px;
      color: #991b1b;
    }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 10px;
    }
    .metric-item { font-size: 13px; }
    .metric-item .label { color: #6b7280; margin-bottom: 2px; }
    .metric-item .value { font-weight: 600; font-size: 15px; color: #111827; }
    .breaches {
      margin-top: 14px;
      font-size: 13px;
      color: #7f1d1d;
    }
    .breach-tag {
      display: inline-block;
      background: #fee2e2;
      color: #b91c1c;
      font-weight: 700;
      border-radius: 4px;
      padding: 2px 8px;
      margin: 2px 3px 2px 0;
      font-size: 12px;
    }

    /* ── All-healthy banner ─────────────────────────────── */
    .healthy-banner {
      margin: 20px 36px;
      padding: 18px 22px;
      background: #f0fdf4;
      border: 1px solid #86efac;
      border-left: 5px solid #22c55e;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 600;
      color: #15803d;
    }

    /* ── Dashboard button ───────────────────────────────── */
    .btn-wrap { padding: 20px 36px; }
    .btn {
      display: inline-block;
      background: #0f3460;
      color: #ffffff !important;
      text-decoration: none;
      padding: 12px 28px;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 600;
      letter-spacing: .3px;
    }
    .btn:hover { background: #16213e; }

    /* ── Full metrics table ─────────────────────────────── */
    .table-wrap { padding: 0 36px 28px; overflow-x: auto; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    thead th {
      background: #f9fafb;
      border-bottom: 2px solid #e5e7eb;
      padding: 10px 12px;
      text-align: left;
      font-weight: 700;
      color: #374151;
      white-space: nowrap;
    }
    tbody td {
      padding: 9px 12px;
      border-bottom: 1px solid #f3f4f6;
      white-space: nowrap;
    }
    /* Color-code rows by status */
    tr.row-alert td { background: #fef2f2; }
    tr.row-ok   td { background: #f0fdf4; }
    .status-alert {
      font-weight: 700;
      color: #b91c1c;
    }
    .status-ok {
      font-weight: 700;
      color: #15803d;
    }

    /* ── Footer ─────────────────────────────────────────── */
    .footer {
      background: #f9fafb;
      border-top: 1px solid #e5e7eb;
      padding: 16px 36px;
      font-size: 12px;
      color: #9ca3af;
      text-align: center;
    }
  </style>
</head>
<body>
<div class="wrapper">

  <!-- ── Header ───────────────────────────────────────────── -->
  <div class="header">
    <h1>📊 Monday Strategy Monitor</h1>
    <p>Weekly Performance Report &nbsp;·&nbsp; {{ run_date }}</p>
  </div>

  <!-- ── Summary pills ────────────────────────────────────── -->
  <div class="summary">
    <span class="pill pill-alert">⚠️ {{ flagged_count }} Flagged</span>
    <span class="pill pill-ok">✅ {{ healthy_count }} Healthy</span>
  </div>

  <!-- ── Flagged strategies ────────────────────────────────── -->
  {% if flagged %}
  <p class="section-title">Strategies Requiring Attention</p>

  {% for name, info in flagged.items() %}
  <div class="alert-card">
    <h3>{{ name }}</h3>
    <div class="metric-grid">
      <div class="metric-item">
        <div class="label">Sharpe Ratio</div>
        <div class="value">{{ info.metrics.sharpe | fmt_num }}</div>
      </div>
      <div class="metric-item">
        <div class="label">Sortino Ratio</div>
        <div class="value">{{ info.metrics.sortino | fmt_num }}</div>
      </div>
      <div class="metric-item">
        <div class="label">Max Drawdown</div>
        <div class="value">{{ info.metrics.max_drawdown | fmt_pct }}</div>
      </div>
      <div class="metric-item">
        <div class="label">CAGR</div>
        <div class="value">{{ info.metrics.cagr | fmt_pct }}</div>
      </div>
      <div class="metric-item">
        <div class="label">Beta</div>
        <div class="value">{{ info.metrics.beta | fmt_num }}</div>
      </div>
      <div class="metric-item">
        <div class="label">Alpha</div>
        <div class="value">{{ info.metrics.alpha | fmt_pct }}</div>
      </div>
      <div class="metric-item">
        <div class="label">Calmar</div>
        <div class="value">{{ info.metrics.calmar | fmt_num }}</div>
      </div>
    </div>
    <div class="breaches">
      <strong>Breached thresholds:</strong>
      {% for b in info.breaches %}
        <span class="breach-tag">{{ b }}</span>
      {% endfor %}
    </div>
  </div>
  {% endfor %}

  {% else %}
  <div class="healthy-banner">
    ✅ All {{ total_count }} strategies are within healthy thresholds.
  </div>
  {% endif %}

  <!-- ── Dashboard button ─────────────────────────────────── -->
  {% if sheet_url %}
  <div class="btn-wrap">
    <a class="btn" href="{{ sheet_url }}">View Dashboard →</a>
  </div>
  {% endif %}

  <!-- ── Full metrics table ────────────────────────────────── -->
  <p class="section-title">All Strategy Metrics</p>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Strategy</th>
          <th>Sharpe</th>
          <th>Sortino</th>
          <th>Max DD</th>
          <th>CAGR</th>
          <th>Beta</th>
          <th>Alpha</th>
          <th>Calmar</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {% for name, metrics in all_metrics.items() %}
        <tr class="{{ 'row-alert' if statuses[name] == 'ALERT' else 'row-ok' }}">
          <td><strong>{{ name }}</strong></td>
          <td>{{ metrics.sharpe      | fmt_num }}</td>
          <td>{{ metrics.sortino     | fmt_num }}</td>
          <td>{{ metrics.max_drawdown | fmt_pct }}</td>
          <td>{{ metrics.cagr        | fmt_pct }}</td>
          <td>{{ metrics.beta        | fmt_num }}</td>
          <td>{{ metrics.alpha       | fmt_pct }}</td>
          <td>{{ metrics.calmar      | fmt_num }}</td>
          <td>
            {% if statuses[name] == 'ALERT' %}
              <span class="status-alert">⚠ ALERT</span>
            {% else %}
              <span class="status-ok">✓ OK</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- ── Footer ───────────────────────────────────────────── -->
  <div class="footer">
    Generated {{ run_date }} &nbsp;·&nbsp; Monday Strategy Monitor
    &nbsp;·&nbsp; Risk-free rate {{ rf_rate }}% &nbsp;·&nbsp;
    Lookback {{ lookback_years }} year(s)
  </div>

</div>
</body>
</html>
"""


# ===========================================================================
# StrategyRunner
# ===========================================================================
class StrategyRunner:
    """
    Fetches market data and simulates strategy returns using vectorbt.

    Each strategy's ``signal`` identifier maps to a private method that
    produces a boolean entry/exit mask over the price series, which is then
    passed to vectorbt's Portfolio.from_signals() for realistic P&L simulation
    including transaction costs.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = config
        self._price_cache: dict[str, pd.Series] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def run_all(self) -> dict[str, pd.Series]:
        """
        Run every strategy in config.  Returns a dict mapping strategy name
        to a daily returns pd.Series (net of fees).
        """
        end   = datetime.today()
        # Extra 60-day buffer so slow MAs have enough warm-up data
        start = end - timedelta(days=int(self.cfg["LOOKBACK_YEARS"] * 365 + 60))

        # Collect all unique tickers (+ SPY benchmark)
        all_tickers: set[str] = {"SPY"}
        for strat in self.cfg["STRATEGIES"]:
            all_tickers.update(strat["tickers"])

        logger.info(
            "Fetching price data via %s for: %s",
            "Alpaca" if _USE_ALPACA else "yfinance",
            sorted(all_tickers),
        )
        self._price_cache = self._fetch_prices(all_tickers, start, end)

        results: dict[str, pd.Series] = {}
        cutoff = end - timedelta(days=int(self.cfg["LOOKBACK_YEARS"] * 365))

        for strat in self.cfg["STRATEGIES"]:
            logger.info("Simulating strategy: %s", strat["name"])
            try:
                ret = self._simulate(strat, self._price_cache)
                # Trim to the requested lookback window
                ret = ret[ret.index >= cutoff.strftime("%Y-%m-%d")]
                results[strat["name"]] = ret
            except Exception as exc:
                logger.error(
                    "Strategy '%s' simulation failed: %s", strat["name"], type(exc).__name__
                )
                # Stack frames only -- traceback.format_exc()'s final line is
                # str(exc), which can embed a full URL/token and would undo
                # the redaction above (codex review discussion_r3742558644).
                logger.debug("".join(traceback.format_tb(exc.__traceback__)))
                results[strat["name"]] = pd.Series(dtype=float)

        return results

    def get_benchmark_returns(self) -> pd.Series:
        """Return daily SPY returns for the lookback period."""
        if "SPY" not in self._price_cache:
            raise RuntimeError(
                "Benchmark data not loaded — call run_all() first."
            )
        spy    = self._price_cache["SPY"]
        end    = datetime.today()
        cutoff = end - timedelta(days=int(self.cfg["LOOKBACK_YEARS"] * 365))
        spy    = spy[spy.index >= cutoff.strftime("%Y-%m-%d")]
        return spy.pct_change().dropna()

    # ── Private helpers ─────────────────────────────────────────────────────

    def _fetch_prices(
        self, tickers: set[str], start: datetime, end: datetime
    ) -> dict[str, pd.Series]:
        """Fetch OHLCV data and return {ticker: close pd.Series}."""
        logger.info("Data source: %s", "Alpaca" if _USE_ALPACA else "yfinance (fallback)")
        if _USE_ALPACA:
            client = StockHistoricalDataClient(_ALPACA_KEY, _ALPACA_SECRET)  # type: ignore
            request = StockBarsRequest(  # type: ignore
                symbol_or_symbols=list(tickers),
                timeframe=TimeFrame.Day,  # type: ignore
                start=start,
                end=end,
                adjustment="all",
            )
            bars = client.get_stock_bars(request)
            bars_df = bars.df
            return self._extract_close_alpaca(bars_df, tickers)
        else:
            raw = yf.download(  # type: ignore
                list(tickers),
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                auto_adjust=True,
                progress=False,
            )
            return self._extract_close(raw, tickers)

    def _extract_close_alpaca(
        self, bars_df: pd.DataFrame, tickers: set[str]
    ) -> dict[str, pd.Series]:
        """Extract close series per ticker from an Alpaca BarSet MultiIndex DataFrame."""
        close = {}
        for ticker in tickers:
            try:
                s = bars_df.loc[ticker]["close"]
                s.index = pd.to_datetime(s.index).tz_localize(None)
                close[ticker] = s.dropna()
            except KeyError:
                logger.warning("No Alpaca price data for: %s", ticker)
        return close

    def _extract_close(
        self, raw: pd.DataFrame, tickers: set[str]
    ) -> dict[str, pd.Series]:
        """
        Normalise yfinance output (single or multi-ticker) into
        {ticker: pd.Series(close)}.
        """
        close: dict[str, pd.Series] = {}
        if isinstance(raw.columns, pd.MultiIndex):
            for ticker in tickers:
                if ("Close", ticker) in raw.columns:
                    close[ticker] = raw[("Close", ticker)].dropna()
                else:
                    logger.warning("No price data returned for ticker: %s", ticker)
        else:
            # Single-ticker download — columns are flat
            ticker = next(iter(tickers))
            close[ticker] = raw["Close"].dropna()
        return close

    def _simulate(
        self, strat: dict, close_all: dict[str, pd.Series]
    ) -> pd.Series:
        """Dispatch to the appropriate signal method and return daily returns."""
        dispatch = {
            "ma_crossover":  self._ma_crossover,
            "rsi_reversion": self._rsi_reversion,
            "momentum":      self._momentum,
            "low_volatility":self._low_volatility,
            "value_factor":  self._value_factor,
        }
        sig = strat["signal"]
        if sig not in dispatch:
            raise ValueError(f"Unknown signal identifier: {sig!r}")
        return dispatch[sig](strat, close_all)

    # ── Signal implementations ───────────────────────────────────────────

    def _ma_crossover(
        self, strat: dict, close_all: dict[str, pd.Series]
    ) -> pd.Series:
        """
        Moving-average crossover: long when fast MA > slow MA, flat otherwise.
        Entry signal fires on the day the fast MA crosses above the slow MA.
        """
        ticker = strat["tickers"][0]
        price  = close_all[ticker]
        fast   = strat["params"]["fast"]
        slow   = strat["params"]["slow"]

        fast_ma = price.rolling(fast).mean()
        slow_ma = price.rolling(slow).mean()

        entries = (fast_ma > slow_ma) & (fast_ma.shift(1) <= slow_ma.shift(1))
        exits   = (fast_ma < slow_ma) & (fast_ma.shift(1) >= slow_ma.shift(1))

        pf = vbt.Portfolio.from_signals(  # type: ignore
            price, entries, exits,
            fees=self.cfg["FEES"], freq="1D", init_cash=10_000,
        )
        return pf.returns()

    def _rsi_reversion(
        self, strat: dict, close_all: dict[str, pd.Series]
    ) -> pd.Series:
        """
        RSI mean-reversion: enter long when RSI crosses below the oversold
        threshold (low), exit when RSI crosses above the overbought threshold.
        """
        ticker  = strat["tickers"][0]
        price   = close_all[ticker]
        period  = strat["params"]["period"]
        low_th  = strat["params"]["low"]
        high_th = strat["params"]["high"]

        rsi = self._compute_rsi(price, period)

        entries = (rsi < low_th)  & (rsi.shift(1) >= low_th)
        exits   = (rsi > high_th) & (rsi.shift(1) <= high_th)

        pf = vbt.Portfolio.from_signals(  # type: ignore
            price, entries, exits,
            fees=self.cfg["FEES"], freq="1D", init_cash=10_000,
        )
        return pf.returns()

    def _momentum(
        self, strat: dict, close_all: dict[str, pd.Series]
    ) -> pd.Series:
        """
        12-1 month momentum: rank tickers by (lookback - skip) month return,
        equal-weight long the top half each day (rebalance signal recomputed daily).
        """
        tickers = strat["tickers"]
        skip    = strat["params"]["skip_months"]
        lb      = strat["params"]["lookback_months"]

        prices = pd.DataFrame(
            {t: close_all[t] for t in tickers if t in close_all}
        ).dropna()

        daily_ret     = prices.pct_change()
        lookback_days = (lb - skip) * 21
        skip_days     = skip * 21

        momentum_score = prices.shift(skip_days) / prices.shift(lookback_days) - 1

        n_long  = max(1, len(tickers) // 2)
        ranks   = momentum_score.rank(axis=1, ascending=False)
        weights = (ranks <= n_long).astype(float)
        weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0)

        port_ret = (weights.shift(1) * daily_ret).sum(axis=1)
        # Approximate fee drag spread across daily steps (~monthly turnover)
        port_ret -= self.cfg["FEES"] * 0.5 / 21
        return port_ret.dropna()

    def _low_volatility(
        self, strat: dict, close_all: dict[str, pd.Series]
    ) -> pd.Series:
        """
        Minimum-variance portfolio: monthly rebalance using the trailing 63-day
        covariance matrix.  Analytic closed-form solution for min-variance weights.
        """
        tickers = strat["tickers"]
        prices  = pd.DataFrame(
            {t: close_all[t] for t in tickers if t in close_all}
        ).dropna()

        daily_ret       = prices.pct_change().dropna()
        cov_window      = 63   # ~3-month trailing covariance estimate
        rebalance_freq  = 21   # ~monthly rebalance

        port_rets = []
        n         = prices.shape[1]
        weights   = np.ones(n) / n   # start equal-weight

        for idx in range(cov_window, len(daily_ret)):
            # Rebalance on schedule
            if (idx - cov_window) % rebalance_freq == 0:
                window_rets = daily_ret.iloc[idx - cov_window : idx].values
                weights     = self._min_variance_weights(window_rets)

            daily_pnl = float(np.dot(weights, daily_ret.iloc[idx].values))
            port_rets.append((daily_ret.index[idx], daily_pnl))

        result = pd.Series(
            [r[1] for r in port_rets],
            index=pd.DatetimeIndex([r[0] for r in port_rets]),
        )
        result -= self.cfg["FEES"] * 0.5 / rebalance_freq
        return result

    def _value_factor(
        self, strat: dict, close_all: dict[str, pd.Series]
    ) -> pd.Series:
        """
        Price-to-52-week-high value proxy: lower ratio = cheaper = more valuable.
        Long the cheapest half of the universe, equal-weighted, monthly rebalance.
        """
        tickers = strat["tickers"]
        prices  = pd.DataFrame(
            {t: close_all[t] for t in tickers if t in close_all}
        ).dropna()

        daily_ret    = prices.pct_change()
        rolling_high = prices.rolling(252).max()
        price_to_hi  = prices / rolling_high   # low ratio → undervalued

        n_long  = max(1, len(tickers) // 2)
        ranks   = price_to_hi.rank(axis=1, ascending=True)  # rank 1 = cheapest
        weights = (ranks <= n_long).astype(float)
        weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0)

        port_ret = (weights.shift(1) * daily_ret).sum(axis=1)
        port_ret -= self.cfg["FEES"] * 0.5 / 21
        return port_ret.dropna()

    # ── Indicator helpers ────────────────────────────────────────────────

    @staticmethod
    def _compute_rsi(price: pd.Series, period: int) -> pd.Series:
        """Wilder-smoothed RSI."""
        delta    = price.diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs       = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _min_variance_weights(returns: np.ndarray) -> np.ndarray:
        """
        Analytic minimum-variance weights.
        Falls back to equal weighting if the covariance matrix is singular.
        """
        n = returns.shape[1]
        try:
            cov     = np.cov(returns, rowvar=False)
            inv_cov = np.linalg.inv(cov)
            ones    = np.ones(n)
            raw     = inv_cov @ ones
            return raw / raw.sum()
        except np.linalg.LinAlgError:
            return np.ones(n) / n


# ===========================================================================
# MetricsCalculator
# ===========================================================================
class MetricsCalculator:
    """
    Computes risk/return metrics for a strategy given its daily return series
    and the SPY benchmark return series.

    All ratios are annualised.  Risk-free rate and trading days taken from config.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg      = config
        self.td       = config["TRADING_DAYS"]
        self.rf_daily = (1 + config["RISK_FREE_RATE"]) ** (1 / self.td) - 1

    # ── Public API ──────────────────────────────────────────────────────────

    def compute(
        self,
        returns:   pd.Series,
        benchmark: pd.Series,
        name:      str,
    ) -> dict[str, float]:
        """
        Compute all metrics.  Returns a flat dict with float values.
        All metrics are NaN when the return series has fewer than 5 observations.
        """
        _nan = {k: float("nan") for k in
                ["sharpe", "sortino", "max_drawdown", "cagr",
                 "beta", "alpha", "calmar"]}

        if returns.empty or len(returns) < 5:
            logger.warning("Insufficient data for %s — returning NaN.", name)
            return _nan

        ret = returns.dropna()
        bm  = benchmark.reindex(ret.index).dropna()
        ret = ret.reindex(bm.index)

        return {
            "sharpe":       self._sharpe(ret),
            "sortino":      self._sortino(ret),
            "max_drawdown": self._max_drawdown(ret),
            "cagr":         self._cagr(ret),
            "beta":         self._beta(ret, bm),
            "alpha":        self._alpha(ret, bm),
            "calmar":       self._calmar(ret),
        }

    # ── Metric methods ───────────────────────────────────────────────────

    def _sharpe(self, ret: pd.Series) -> float:
        """Annualised Sharpe Ratio: (mean excess return / std) × √252."""
        excess = ret - self.rf_daily
        if excess.std() == 0:
            return float("nan")
        return float((excess.mean() / excess.std()) * np.sqrt(self.td))

    def _sortino(self, ret: pd.Series) -> float:
        """
        Annualised Sortino Ratio.
        Downside deviation uses only returns below the daily risk-free rate.
        """
        excess   = ret - self.rf_daily
        downside = excess[excess < 0]
        if len(downside) == 0 or downside.std() == 0:
            return float("nan")
        return float((excess.mean() / downside.std()) * np.sqrt(self.td))

    def _max_drawdown(self, ret: pd.Series) -> float:
        """
        Maximum drawdown as a negative fraction.
        -0.20 = 20% peak-to-trough decline.
        """
        cum         = (1 + ret).cumprod()
        rolling_max = cum.cummax()
        drawdown    = (cum - rolling_max) / rolling_max
        return float(drawdown.min())

    def _cagr(self, ret: pd.Series) -> float:
        """Compound Annual Growth Rate over the observed period."""
        n     = len(ret)
        total = (1 + ret).prod()
        return float(total ** (self.td / n) - 1)

    def _beta(self, ret: pd.Series, bm: pd.Series) -> float:
        """OLS beta of strategy vs benchmark daily returns."""
        if len(ret) < 3:
            return float("nan")
        slope, *_ = stats.linregress(bm.values, ret.values)  # type: ignore
        return float(slope)

    def _alpha(self, ret: pd.Series, bm: pd.Series) -> float:
        """
        Jensen's Alpha (annualised):
            α = E[r_p] − [r_f + β·(E[r_m] − r_f)]
        """
        beta = self._beta(ret, bm)
        if np.isnan(beta):
            return float("nan")
        alpha_daily = (
            ret.mean() - (self.rf_daily + beta * (bm.mean() - self.rf_daily))
        )
        return float(alpha_daily * self.td)

    def _calmar(self, ret: pd.Series) -> float:
        """Calmar Ratio = CAGR / |Max Drawdown|."""
        cagr = self._cagr(ret)
        mdd  = self._max_drawdown(ret)
        if mdd == 0:
            return float("nan")
        return cagr / abs(mdd)


# ===========================================================================
# CSVExporter
# ===========================================================================
class CSVExporter:
    """
    Writes one row per strategy to a date-stamped CSV file in the configured
    output directory.  The CSV acts as the local dashboard artefact.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg        = config
        self.output_dir = config.get("CSV_OUTPUT_DIR", ".")

    def export(
        self,
        all_metrics: dict[str, dict[str, float]],
        statuses:    dict[str, str],
        run_date:    str,
    ) -> str:
        """
        Write the CSV file and return its absolute path.
        Raises OSError if the output directory cannot be created.
        """
        os.makedirs(self.output_dir, exist_ok=True)
        filename = f"strategy_report_{run_date}.csv"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(_REPORT_COLUMNS)

            for name, metrics in all_metrics.items():
                writer.writerow([
                    run_date,
                    name,
                    self._fmt(metrics.get("sharpe")),
                    self._fmt(metrics.get("sortino")),
                    self._fmt(metrics.get("max_drawdown")),
                    self._fmt(metrics.get("cagr")),
                    self._fmt(metrics.get("beta")),
                    self._fmt(metrics.get("alpha")),
                    self._fmt(metrics.get("calmar")),
                    statuses.get(name, "OK"),
                ])

        logger.info("CSV report written: %s", filepath)
        return os.path.abspath(filepath)

    @staticmethod
    def _fmt(v: float | None) -> str:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return ""
        return f"{v:.6f}"



# ===========================================================================
# SheetsWriter
# ===========================================================================
class SheetsWriter:
    """
    Appends weekly metric rows to the pre-configured Google Sheet via the
    Pipedream connector REST API — no gspread or service-account JSON needed.

    Requires PIPEDREAM_TOKEN environment variable (your Pipedream API key).
    Spreadsheet ID and sheet name come from CONFIG.

    Row schema: Date | Strategy | Sharpe | Sortino | MaxDD | CAGR | Beta | Alpha | Calmar | Status
    """

    _API_BASE = "https://api.pipedream.com/v1/accounts/google_sheets__pipedream/actions"

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg            = config
        self.spreadsheet_id = config.get("GOOGLE_SHEET_ID", "")
        self.sheet_name     = config.get("GOOGLE_SHEET_NAME", "Weekly_Report")
        self.token          = config.get("PIPEDREAM_TOKEN", "")

    def _sanitize(self, body: str) -> str:
        """Sanitize response bodies before logging to prevent token leakage."""
        if not body:
            return ""

        # Mask the explicit token if known
        if self.token:
            body = body.replace(str(self.token), "***")

        # Mask generic Bearer tokens
        import re
        body = re.sub(r'Bearer\s+[A-Za-z0-9\-._~+/=]+', 'Bearer ***', body)

        # Truncate to avoid log spam / filling buffers
        max_len = 500
        if len(body) > max_len:
            body = body[:max_len] + "... [truncated]"

        return body

    def write_rows(
        self,
        all_metrics: dict[str, dict[str, float]],
        statuses:    dict[str, str],
        run_date:    str,
    ) -> bool:
        """
        Append one row per strategy to the Google Sheet.
        Returns True on success, False on any failure.
        """
        import urllib.error
        import urllib.request
        from urllib.parse import urlparse

        # `urlopen` follows a 30x `Location` header by default and preserves
        # the `Authorization` header while doing so — a compromised or
        # misconfigured upstream could redirect this request to an internal
        # host (or an ftp://file:// URL) and receive the Pipedream bearer
        # token. `_API_BASE` never legitimately redirects, so treat any
        # redirect attempt as a failure instead of validating and following it.
        class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise urllib.error.HTTPError(
                    newurl, code, "SheetsWriter: refusing to follow redirect", headers, fp
                )

        if not self.spreadsheet_id:
            logger.warning("SheetsWriter: no GOOGLE_SHEET_ID configured — skipping.")
            return False
        if not self.token:
            logger.warning(
                "SheetsWriter: PIPEDREAM_TOKEN not set — "
                "skipping Google Sheets write. Set the env var to enable."
            )
            return False

        rows = []
        for name, metrics in all_metrics.items():
            rows.append({
                "Date":     run_date,
                "Strategy": name,
                "Sharpe":   self._fmt(metrics.get("sharpe")),
                "Sortino":  self._fmt(metrics.get("sortino")),
                "MaxDD":    self._fmt(metrics.get("max_drawdown")),
                "CAGR":     self._fmt(metrics.get("cagr")),
                "Beta":     self._fmt(metrics.get("beta")),
                "Alpha":    self._fmt(metrics.get("alpha")),
                "Calmar":   self._fmt(metrics.get("calmar")),
                "Status":   statuses.get(name, "OK"),
            })

        url = f"{self._API_BASE}/add-rows"

        parsed_url = urlparse(url)
        if parsed_url.scheme.lower() != "https" or parsed_url.netloc.lower() != "api.pipedream.com":
            logger.error("SheetsWriter: blocked unsafe Sheets API URL %s", url)
            return False

        from dca_net_utils import assert_safe_destination as _assert_safe_destination
        try:
            _assert_safe_destination(url)
        except Exception as exc:
            logger.error("SheetsWriter: blocked unsafe Sheets API URL %s (%s)", url, exc)
            return False

        payload = json.dumps({
            "spreadsheetId": self.spreadsheet_id,
            "worksheetId":   self.sheet_name,
            "rows":          rows,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type":  "application/json",
            },
        )

        try:
            from dca_net_utils import BlockedFileHandler, BlockedFTPHandler, PinnedHTTPHandler, PinnedHTTPSHandler
            opener = urllib.request.build_opener(_NoRedirectHandler, PinnedHTTPHandler, PinnedHTTPSHandler, BlockedFileHandler, BlockedFTPHandler)
            with opener.open(req, timeout=20) as resp:  # noqa: S310
                status_code = resp.status
                if status_code in (200, 201):
                    logger.info(
                        "SheetsWriter: %d rows written to '%s' (HTTP %d).",
                        len(rows), self.sheet_name, status_code,
                    )
                    return True
                else:
                    body = resp.read().decode("utf-8", errors="replace")
                    logger.error("SheetsWriter: unexpected HTTP %d — %s", status_code, self._sanitize(body))
                    return False
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("SheetsWriter HTTP error %d: %s", exc.code, self._sanitize(body))
            return False
        except Exception as exc:
            logger.error("SheetsWriter failed: %s", self._sanitize(str(exc)))
            return False

    @staticmethod
    def _fmt(v) -> str:
        if v is None or (isinstance(v, float) and (v != v)):  # NaN check
            return ""
        return f"{v:.6f}"


# ===========================================================================
# EmailNotifier
# ===========================================================================
class EmailNotifier:
    """
    Builds and sends an HTML email report via SMTP (Gmail-compatible STARTTLS).

    The email contains:
      • Summary header (N flagged / M healthy)
      • Per-flagged-strategy section with metric values and breach tags
      • Optional Google Sheet dashboard button
      • Full color-coded metrics table for all strategies
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = config

        # Compile the Jinja2 template once at construction time
        env = Environment(loader=BaseLoader(), autoescape=True)  # type: ignore

        # Custom filters used in the template
        def fmt_num(v: float, decimals: int = 3) -> str:
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "N/A"
            return f"{v:.{decimals}f}"

        def fmt_pct(v: float) -> str:
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "N/A"
            return f"{v * 100:.2f}%"

        env.filters["fmt_num"] = fmt_num
        env.filters["fmt_pct"] = fmt_pct

        self._template = env.from_string(_EMAIL_TEMPLATE)

    # ── Public API ──────────────────────────────────────────────────────────

    def build_html(
        self,
        flagged:       dict[str, dict],
        all_metrics:   dict[str, dict[str, float]],
        statuses:      dict[str, str],
        sheet_url:     str,
        run_date:      str,
    ) -> str:
        """Render and return the HTML email body string."""
        healthy_count = len(all_metrics) - len(flagged)
        return self._template.render(
            run_date      = run_date,
            flagged       = flagged,
            all_metrics   = all_metrics,
            statuses      = statuses,
            sheet_url     = sheet_url,
            flagged_count = len(flagged),
            healthy_count = healthy_count,
            total_count   = len(all_metrics),
            rf_rate       = int(self.cfg["RISK_FREE_RATE"] * 100),
            lookback_years= self.cfg["LOOKBACK_YEARS"],
        )

    def build_subject(self, flagged_count: int, run_date: str) -> str:
        """Build the email subject line."""
        return (
            f"📊 Monday Strategy Monitor — {run_date} "
            f"| {flagged_count} strateg"
            f"{'y' if flagged_count == 1 else 'ies'} flagged"
        )

    def send(
        self,
        html_body: str,
        subject:   str,
    ) -> None:
        """
        Send the HTML email via SMTP with STARTTLS.
        Raises ValueError  if required env vars are missing.
        Raises smtplib.*   on SMTP connection or authentication failures.
        """
        smtp_host  = self.cfg["SMTP_HOST"]
        smtp_port  = self.cfg["SMTP_PORT"]
        smtp_user  = self.cfg["SMTP_USER"]
        smtp_pass  = self.cfg["SMTP_PASSWORD"]
        recipient  = self.cfg["RECIPIENT_EMAIL"]

        missing = [
            k for k, v in {
                "SMTP_USER": smtp_user,
                "SMTP_PASSWORD": smtp_pass,
                "RECIPIENT_EMAIL": recipient,
            }.items() if not v
        ]
        if missing:
            raise ValueError(
                f"Missing required email config: {', '.join(missing)}. "
                "Set the corresponding environment variables."
            )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = recipient
        # Plain-text fallback for clients that don't render HTML
        plain = (
            f"Monday Strategy Monitor — {subject}\n\n"
            "This report requires an HTML-capable email client.\n"
            f"Dashboard: {self.cfg.get('GOOGLE_SHEET_URL', '(not set)')}"
        )
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        logger.info(
            "Connecting to SMTP %s:%d as %s …", smtp_host, smtp_port, smtp_user
        )
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())

        logger.info("Email sent to %s.", recipient)


# ===========================================================================
# MonitorOrchestrator
# ===========================================================================
class MonitorOrchestrator:
    """
    Top-level coordinator that wires together:
      StrategyRunner   → simulates strategy returns
      MetricsCalculator→ computes risk/return metrics
      CSVExporter      → writes the local CSV report
      EmailNotifier    → renders and sends the HTML email

    Call ``run()`` from the CLI entry point.
    """

    def __init__(
        self,
        config:      dict[str, Any],
        dry_run:     bool = False,
        force_alert: bool = False,
    ) -> None:
        self.cfg         = config
        self.dry_run     = dry_run
        self.force_alert = force_alert

        self.runner   = StrategyRunner(config)
        self.calc     = MetricsCalculator(config)
        self.exporter = CSVExporter(config)
        self.emailer  = EmailNotifier(config)
        self.sheets   = SheetsWriter(config)

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self) -> int:
        """
        Main entry point.
        Returns 0 on full success, 1 if any non-fatal component failed.
        """
        run_date  = date.today().isoformat()
        exit_code = 0

        logger.info("=== Strategy Monitor run: %s ===", run_date)

        # ── Step 1: Simulate strategies ──────────────────────────────────
        logger.info("Step 1/4 — Simulating strategies …")
        strategy_returns  = self.runner.run_all()
        benchmark_returns = self.runner.get_benchmark_returns()

        # ── Step 2: Compute metrics ──────────────────────────────────────
        logger.info("Step 2/4 — Computing metrics …")
        all_metrics: dict[str, dict[str, float]] = {}
        for name, returns in strategy_returns.items():
            all_metrics[name] = self.calc.compute(
                returns, benchmark_returns, name
            )

        # ── Step 3: Evaluate thresholds ──────────────────────────────────
        logger.info("Step 3/4 — Evaluating thresholds …")
        thresholds = self.cfg["THRESHOLDS"]
        flagged:   dict[str, dict] = {}
        statuses:  dict[str, str]  = {}

        for name, metrics in all_metrics.items():
            breaches = self._check_thresholds(metrics, thresholds)
            if breaches:
                flagged[name] = {"metrics": metrics, "breaches": breaches}
                statuses[name] = "ALERT"
                logger.info("  ALERT  %s → breached: %s", name, breaches)
            else:
                statuses[name] = "OK"
                logger.info("  OK     %s", name)

        healthy_count = len(all_metrics) - len(flagged)

        # ── Step 4a: Export CSV ──────────────────────────────────────────
        logger.info("Step 4/4a — Exporting CSV and writing to Google Sheets …")
        csv_path = ""
        if not self.dry_run:
            try:
                csv_path = self.exporter.export(all_metrics, statuses, run_date)
            except Exception as exc:
                logger.error("CSV export failed: %s — continuing.", exc)
                exit_code = 1
            # Write metrics to Google Sheets (non-fatal if it fails)
            try:
                ok = self.sheets.write_rows(all_metrics, statuses, run_date)
                if not ok:
                    logger.warning("Google Sheets write did not confirm success.")
            except Exception as exc:
                logger.error("Google Sheets write failed: %s — continuing to email.", exc)
                exit_code = 1
        else:
            self._print_dry_run_csv(all_metrics, statuses, run_date)
            logger.info("[dry-run] Google Sheets write skipped.")

        # ── Step 4b: Build & send email ──────────────────────────────────
        logger.info("Step 4/4b — Preparing email …")
        sheet_url = (self.cfg.get("GOOGLE_SHEET_URL", "") or "").strip()
        if sheet_url and not sheet_url.lower().startswith(("http://", "https://")):
            logger.warning("Invalid GOOGLE_SHEET_URL scheme; omitting dashboard link.")
            sheet_url = ""

        # Decide whether to send
        should_send = (
            self.force_alert
            or bool(flagged)
            or self.cfg.get("EMAIL_ON_ALL", False)
        )

        html_body = self.emailer.build_html(
            flagged, all_metrics, statuses, sheet_url, run_date
        )
        subject = self.emailer.build_subject(len(flagged), run_date)

        if not self.dry_run:
            if should_send:
                try:
                    self.emailer.send(html_body, subject)
                except Exception as exc:
                    safe_exc = str(exc)
                    safe_traceback = traceback.format_exc()
                    if self.cfg.get("SMTP_PASSWORD"):
                        smtp_pw = str(self.cfg["SMTP_PASSWORD"])
                        safe_exc = safe_exc.replace(smtp_pw, "***")
                        safe_traceback = safe_traceback.replace(smtp_pw, "***")
                    logger.error("Email send failed: %s", safe_exc)
                    logger.debug(safe_traceback)
                    exit_code = 1
            else:
                logger.info(
                    "No thresholds breached and EMAIL_ON_ALL=False "
                    "— email skipped."
                )
        else:
            self._print_dry_run_email(html_body, subject, should_send, csv_path)

        logger.info(
            "=== Run complete — %d flagged, %d healthy. Exit code: %d ===",
            len(flagged), healthy_count, exit_code,
        )
        return exit_code

    # ── Private helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _check_thresholds(
        metrics:    dict[str, float],
        thresholds: dict[str, float],
    ) -> list[str]:
        """
        Return the list of threshold names that are breached.
        A metric breaches when its value is strictly less than the threshold.
        For max_drawdown the threshold is already negative (e.g. -0.15),
        so the same comparison applies: flag when drawdown < -0.15.
        NaN values are always flagged.
        """
        breaches = []
        for key, threshold in thresholds.items():
            value = metrics.get(key, float("nan"))
            if isinstance(value, float) and np.isnan(value):
                breaches.append(f"{key}(NaN)")
                continue
            if value < threshold:
                breaches.append(key)
        return breaches

    def _print_dry_run_csv(
        self,
        all_metrics: dict[str, dict],
        statuses:    dict[str, str],
        run_date:    str,
    ) -> None:
        """Print the CSV rows that would be written."""
        print("\n" + "=" * 72)
        print("DRY RUN — CSV rows that WOULD be written to "
              f"strategy_report_{run_date}.csv")
        print("=" * 72)
        print(",".join(_REPORT_COLUMNS))
        print("-" * 72)
        for name, metrics in all_metrics.items():
            row = [
                run_date, name,
                self._fmt_cell(metrics.get("sharpe")),
                self._fmt_cell(metrics.get("sortino")),
                self._fmt_cell(metrics.get("max_drawdown")),
                self._fmt_cell(metrics.get("cagr")),
                self._fmt_cell(metrics.get("beta")),
                self._fmt_cell(metrics.get("alpha")),
                self._fmt_cell(metrics.get("calmar")),
                statuses.get(name, "OK"),
            ]
            print(",".join(row))
        print()

    @staticmethod
    def _print_dry_run_email(
        html_body:   str,
        subject:     str,
        would_send:  bool,
        csv_path:    str,
    ) -> None:
        """Print the email subject and full HTML body."""
        print("\n" + "=" * 72)
        action = "WOULD be sent" if would_send else "would NOT be sent"
        print(f"DRY RUN — Email ({action})")
        print("=" * 72)
        print(f"Subject : {subject}")
        if csv_path:
            print(f"CSV path: {csv_path}")
        print("-" * 72)
        print("--- HTML BODY ---")
        print(html_body)
        print()

    @staticmethod
    def _fmt_cell(v: float | None) -> str:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return ""
        return f"{v:.6f}"


# ===========================================================================
# CLI entry point
# ===========================================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monday Strategy Monitor — weekly quant strategy health check.\n"
            "Exports a CSV report and optionally sends an HTML email via SMTP."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Print the email HTML and CSV rows to stdout.\n"
            "Does NOT write files or send email."
        ),
    )
    parser.add_argument(
        "--force-alert",
        action="store_true",
        default=False,
        help="Send the email even if no strategies breach thresholds.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "Path to a JSON file with CONFIG overrides (shallow-merged).\n"
            'Example: {"THRESHOLDS": {"sharpe": 0.3}, "EMAIL_ON_ALL": true}'
        ),
    )
    return parser.parse_args()


def _apply_config_overrides(
    path: str, base: dict[str, Any]
) -> dict[str, Any]:
    """
    Load JSON from ``path`` and shallow-merge into ``base``.
    Nested dicts (e.g. THRESHOLDS) are merged one level deep.
    """
    with open(path, "r", encoding="utf-8") as fh:
        overrides = json.load(fh)

    merged = dict(base)
    for key, val in overrides.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(val, dict)
        ):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val

    # Normalize configured secrets to strings if provided
    for secret_key in ("SMTP_PASSWORD", "PIPEDREAM_TOKEN"):
        if secret_key in merged and merged[secret_key] is not None:
            merged[secret_key] = str(merged[secret_key])

    return merged


if __name__ == "__main__":
    args = _parse_args()

    # Apply optional config-file overrides
    effective_config = CONFIG
    if args.config:
        try:
            effective_config = _apply_config_overrides(args.config, CONFIG)
            logger.info("Config overrides loaded from: %s", args.config)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load config file '%s': %s", args.config, e
            )
            sys.exit(1)

    orchestrator = MonitorOrchestrator(
        config      = effective_config,
        dry_run     = args.dry_run,
        force_alert = args.force_alert,
    )

    try:
        code = orchestrator.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)
    except Exception as exc:
        safe_exc = str(exc)
        safe_traceback = traceback.format_exc()
        if effective_config.get("SMTP_PASSWORD"):
            smtp_pw = str(effective_config["SMTP_PASSWORD"])
            safe_exc = safe_exc.replace(smtp_pw, "***")
            safe_traceback = safe_traceback.replace(smtp_pw, "***")
        if effective_config.get("PIPEDREAM_TOKEN"):
            pipedream_tok = str(effective_config["PIPEDREAM_TOKEN"])
            safe_exc = safe_exc.replace(pipedream_tok, "***")
            safe_traceback = safe_traceback.replace(pipedream_tok, "***")
        logger.critical("Unhandled exception: %s", safe_exc)
        logger.debug(safe_traceback)
        sys.exit(1)

    sys.exit(code)
