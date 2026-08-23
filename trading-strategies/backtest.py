#!/usr/bin/env python3
"""
task1_backtest.py
=================
Production-quality backtesting script using vectorbt.

Strategies implemented:
  1. Moving Average Crossover (Golden/Death Cross)
  2. RSI Mean Reversion (Oversold/Overbought)

Requirements:
    alpaca-py>=0.8.0
    vectorbt>=0.25.0
    yfinance>=0.2.0
    pandas>=1.5.0
    numpy>=1.23.0
    pandas-ta>=0.3.14b
    rich>=13.0.0   (optional, for pretty tables; falls back gracefully)

Usage examples:
    python task1_backtest.py
    python task1_backtest.py --strategy ma_crossover --tickers AAPL MSFT --fast-window 20 --slow-window 50
    python task1_backtest.py --strategy rsi_reversion --tickers AAPL NVDA --rsi-period 14 --rsi-low 30 --rsi-high 70
    python task1_backtest.py --strategy both --start 2020-01-01 --end 2025-01-01 --commission 0.001 --slippage 0.0005
"""

# Alpaca API credentials (set these env vars for real-time-ready data)
# export ALPACA_API_KEY=your_key
# export ALPACA_SECRET_KEY=your_secret
# Falls back to yfinance if not set.

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import argparse
import os
import sys
import warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Third-party
# ---------------------------------------------------------------------------
import numpy as np
import pandas as pd
try:
    from alpaca.data.historical import StockHistoricalDataClient  # type: ignore
    from alpaca.data.requests import StockBarsRequest  # type: ignore
    from alpaca.data.timeframe import TimeFrame  # type: ignore
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    StockHistoricalDataClient = None
    StockBarsRequest = None
    TimeFrame = None

# Suppress noisy deprecation warnings from vectorbt / pandas internals
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

try:
    import pandas_ta as ta  # type: ignore
except ImportError:
    print("[ERROR] pandas-ta is not installed. Run: pip install pandas-ta")
    sys.exit(1)

try:
    import vectorbt as vbt  # type: ignore
except ImportError:
    print("[ERROR] vectorbt is not installed. Run: pip install vectorbt")
    sys.exit(1)

# Optional rich for prettier console output
try:
    from rich import box  # type: ignore
    from rich.console import Console  # type: ignore
    from rich.table import Table  # type: ignore
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants / Defaults
# ---------------------------------------------------------------------------
DEFAULT_TICKERS      = ["AAPL", "MSFT", "GOOGL", "NVDA", "SPY"]
DEFAULT_START        = (datetime.today() - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
DEFAULT_END          = datetime.today().strftime("%Y-%m-%d")
DEFAULT_FAST_WINDOW  = 50
DEFAULT_SLOW_WINDOW  = 200
DEFAULT_RSI_PERIOD   = 14
DEFAULT_RSI_LOW      = 30
DEFAULT_RSI_HIGH     = 70
DEFAULT_COMMISSION   = 0.001   # 10 bps per trade (one-way)
DEFAULT_SLIPPAGE     = 0.0005  # 5 bps price impact on entry/exit
BENCHMARK_TICKER     = "SPY"


# ===========================================================================
# SECTION 1 — DATA FETCHING
# ===========================================================================

def fetch_data(
    tickers: List[str],
    start: str,
    end: str,
) -> Dict[str, pd.DataFrame]:
    """
    Download daily OHLCV data for each ticker.

    Uses Alpaca Historical Data API (free tier, no key required for IEX feed;
    SIP feed requires paid subscription). Falls back to yfinance if
    ALPACA_API_KEY/ALPACA_SECRET_KEY env vars are not set.

    Parameters
    ----------
    tickers : list of str
        Ticker symbols to download.
    start : str
        Start date in YYYY-MM-DD format.
    end : str
        End date in YYYY-MM-DD format.

    Returns
    -------
    dict
        Mapping of ticker -> DataFrame with columns [Open, High, Low, Close, Volume].
        Tickers that fail to download are omitted with a warning.
    """
    # Ensure the benchmark is always included for comparison
    all_tickers = list(set(tickers + [BENCHMARK_TICKER]))

    api_key    = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    if not ALPACA_AVAILABLE or not api_key or not secret_key:
        if not ALPACA_AVAILABLE:
            print("  [WARN] alpaca-py not installed — falling back to yfinance.")
        else:
            print("  [WARN] ALPACA_API_KEY / ALPACA_SECRET_KEY not set — falling back to yfinance.")
        _print_section("Fetching price data via yfinance (fallback)")

        import yfinance as yf  # type: ignore

        raw = yf.download(
            tickers=all_tickers,
            start=start,
            end=end,
            auto_adjust=True,   # Adjust for splits and dividends
            progress=False,
            threads=True,
        )

        data: Dict[str, pd.DataFrame] = {}

        if isinstance(raw.columns, pd.MultiIndex):
            # Multiple tickers: columns are (field, ticker)
            for ticker in all_tickers:
                try:
                    df = raw.xs(ticker, level=1, axis=1)[["Open", "High", "Low", "Close", "Volume"]]
                    df = df.dropna(how="all")
                    if df.empty:
                        print(f"  [WARN] No data returned for {ticker} — skipping.")
                        continue
                    data[ticker] = df
                    print(f"  {ticker}: {len(df)} rows  ({df.index[0].date()} → {df.index[-1].date()})")
                except KeyError:
                    print(f"  [WARN] {ticker} not found in downloaded data — skipping.")
        else:
            # Single ticker returned as flat DataFrame
            ticker = all_tickers[0]
            df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
            if not df.empty:
                data[ticker] = df
                print(f"  {ticker}: {len(df)} rows  ({df.index[0].date()} → {df.index[-1].date()})")

        return data

    # -----------------------------------------------------------------------
    # Alpaca path
    # -----------------------------------------------------------------------
    _print_section("Fetching price data via Alpaca")

    client = StockHistoricalDataClient(api_key, secret_key)  # type: ignore

    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt   = datetime.strptime(end,   "%Y-%m-%d")

    request = StockBarsRequest(  # type: ignore
        symbol_or_symbols=all_tickers,
        timeframe=TimeFrame.Day,  # type: ignore
        start=start_dt,
        end=end_dt,
        adjustment="all",   # split + dividend adjusted (equivalent to yfinance auto_adjust=True)
    )

    bars = client.get_stock_bars(request)
    df_all = bars.df  # MultiIndex: (symbol, timestamp)

    data: Dict[str, pd.DataFrame] = {}
    for ticker in all_tickers:
        try:
            df = df_all.loc[ticker].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
            if df.empty:
                print(f"  [WARN] No data returned for {ticker} — skipping.")
                continue
            data[ticker] = df
            print(f"  {ticker}: {len(df)} rows  ({df.index[0].date()} → {df.index[-1].date()})")
        except KeyError:
            print(f"  [WARN] {ticker} not found in Alpaca response — skipping.")

    return data


# ===========================================================================
# SECTION 2 — SIGNAL GENERATION
# ===========================================================================

def generate_ma_crossover_signals(
    close: pd.Series,
    fast_window: int = DEFAULT_FAST_WINDOW,
    slow_window: int = DEFAULT_SLOW_WINDOW,
) -> Tuple[pd.Series, pd.Series]:
    """
    Generate entry/exit signals for a Moving Average Crossover strategy.

    Logic
    -----
    * Entry (long):  fast MA crosses *above* slow MA  → Golden Cross
    * Exit  (long):  fast MA crosses *below* slow MA  → Death Cross

    Slippage is NOT applied here; it is applied in the portfolio construction
    step so that the price arrays (not signal booleans) carry the cost.

    Parameters
    ----------
    close       : pd.Series  Daily close prices.
    fast_window : int        Lookback for the fast moving average.
    slow_window : int        Lookback for the slow moving average.

    Returns
    -------
    entries, exits : pd.Series[bool]
    """
    fast_ma = close.rolling(fast_window).mean()
    slow_ma = close.rolling(slow_window).mean()

    # Cross-above: fast was <= slow yesterday, fast > slow today
    entries = (fast_ma > slow_ma) & (fast_ma.shift(1) <= slow_ma.shift(1))
    # Cross-below: fast was >= slow yesterday, fast < slow today
    exits   = (fast_ma < slow_ma) & (fast_ma.shift(1) >= slow_ma.shift(1))

    return entries.fillna(False), exits.fillna(False)


def generate_rsi_reversion_signals(
    close: pd.Series,
    rsi_period: int = DEFAULT_RSI_PERIOD,
    rsi_low:    int = DEFAULT_RSI_LOW,
    rsi_high:   int = DEFAULT_RSI_HIGH,
) -> Tuple[pd.Series, pd.Series]:
    """
    Generate entry/exit signals for an RSI Mean-Reversion strategy.

    Logic
    -----
    * Entry (long):  RSI crosses *above* rsi_low  (exits oversold territory)
    * Exit  (long):  RSI crosses *above* rsi_high (enters overbought territory)

    Parameters
    ----------
    close      : pd.Series  Daily close prices.
    rsi_period : int        RSI calculation period (Wilder smoothing).
    rsi_low    : int        Oversold threshold (e.g. 30).
    rsi_high   : int        Overbought threshold (e.g. 70).

    Returns
    -------
    entries, exits : pd.Series[bool]
    """
    # pandas-ta computes RSI with Wilder smoothing by default
    rsi = ta.rsi(close, length=rsi_period)

    # Entry: RSI was below rsi_low yesterday, now >= rsi_low  (bounce from oversold)
    entries = (rsi >= rsi_low) & (rsi.shift(1) < rsi_low)
    # Exit:  RSI was below rsi_high yesterday, now >= rsi_high (reached overbought)
    exits   = (rsi >= rsi_high) & (rsi.shift(1) < rsi_high)

    return entries.fillna(False), exits.fillna(False)


# ===========================================================================
# SECTION 3 — SLIPPAGE HELPER
# ===========================================================================

def apply_slippage(
    price: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    slippage: float,
) -> Tuple[pd.Series, pd.Series]:
    """
    Adjust entry and exit prices to reflect market-impact slippage.

    On entry  (buy)  : we pay *more*  → price * (1 + slippage)
    On exit   (sell) : we receive *less* → price * (1 - slippage)

    Non-signal bars keep the original close price (vectorbt ignores those
    bars anyway because entries/exits are False there).

    Parameters
    ----------
    price    : pd.Series  Raw close prices.
    entries  : pd.Series[bool]
    exits    : pd.Series[bool]
    slippage : float      Fractional slippage (e.g. 0.0005 = 5 bps).

    Returns
    -------
    entry_price, exit_price : pd.Series
        Price series to pass to Portfolio.from_signals() via the
        ``open`` / ``close`` parameters (or custom price kwargs).
    """
    entry_price = price.copy()
    exit_price  = price.copy()

    entry_price[entries] = price[entries] * (1.0 + slippage)
    exit_price[exits]    = price[exits]   * (1.0 - slippage)

    return entry_price, exit_price


# ===========================================================================
# SECTION 4 — PORTFOLIO CONSTRUCTION
# ===========================================================================

def build_portfolio(
    close: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    commission: float,
    slippage: float,
    init_cash: float = 10_000.0,
) -> "vbt.Portfolio":
    """
    Construct a vectorbt Portfolio from boolean entry/exit signal series.

    Transaction costs
    -----------------
    * Commission : applied via vectorbt's built-in ``fees`` parameter
                   (charged as a fraction of the trade value each way).
    * Slippage   : applied manually as a price offset on the signal bar
                   *before* passing prices to vectorbt.

    Parameters
    ----------
    close      : pd.Series  Close prices (index = DatetimeIndex).
    entries    : pd.Series[bool]
    exits      : pd.Series[bool]
    commission : float      Fractional commission per trade (e.g. 0.001).
    slippage   : float      Fractional price impact per trade (e.g. 0.0005).
    init_cash  : float      Starting portfolio value in USD.

    Returns
    -------
    vbt.Portfolio
    """
    # Compute slippage-adjusted entry and exit price arrays
    entry_price, exit_price = apply_slippage(close, entries, exits, slippage)

    # vectorbt Portfolio.from_signals():
    #   - entries/exits are boolean Series aligned to the price index
    #   - fees=commission applies the cost fraction symmetrically on open/close
    #   - We pass price= (the mid-price) for sizing; actual fill uses
    #     open_price / close_price overrides where available.
    portfolio = vbt.Portfolio.from_signals(
        close=close,
        entries=entries,
        exits=exits,
        price=entry_price,          # Entry fill price (with slippage baked in)
        fees=commission,            # Vectorbt applies this as a fraction of trade value
        init_cash=init_cash,
        freq="D",                   # Daily frequency for annualisation
        # Prevent accumulating multiple long positions without an intervening exit
        accumulate=False,
    )
    return portfolio


def build_benchmark_portfolio(
    close: pd.Series,
    commission: float,
    init_cash: float = 10_000.0,
) -> "vbt.Portfolio":
    """
    Construct a simple buy-and-hold portfolio for SPY as a benchmark.

    Buys on the first available bar and never sells.

    Parameters
    ----------
    close      : pd.Series  SPY close prices.
    commission : float      One-way commission fraction.
    init_cash  : float      Starting cash.

    Returns
    -------
    vbt.Portfolio
    """
    entries = pd.Series(False, index=close.index, dtype=bool)
    exits   = pd.Series(False, index=close.index, dtype=bool)
    entries.iat[0] = True  # Buy on day 1

    portfolio = vbt.Portfolio.from_signals(
        close=close,
        entries=entries,
        exits=exits,
        fees=commission,
        init_cash=init_cash,
        freq="D",
        accumulate=False,
    )
    return portfolio


# ===========================================================================
# SECTION 5 — METRICS EXTRACTION
# ===========================================================================

def extract_metrics(portfolio: "vbt.Portfolio", label: str = "") -> Dict:
    """
    Pull key performance metrics from a vectorbt Portfolio object.

    Uses vectorbt's built-in ``stats()`` method where possible and falls
    back to manual calculation for metrics not exposed by stats().

    Metrics returned
    ----------------
    Sharpe Ratio, Sortino Ratio, Max Drawdown, CAGR, Calmar Ratio,
    Win Rate, Total Trades.

    Parameters
    ----------
    portfolio : vbt.Portfolio
    label     : str  Human-readable label for display.

    Returns
    -------
    dict with the extracted metrics.
    """
    try:
        stats = portfolio.stats()
    except Exception as e:
        print(f"  [WARN] stats() failed for {label}: {e}")
        stats = pd.Series(dtype=float)

    def _get(stat_name: str, default=np.nan):
        """Safe accessor for vectorbt stats Series."""
        try:
            val = stats.get(stat_name, default)
            return float(val) if val is not None else default
        except Exception:
            return default

    # ---- Sharpe Ratio -------------------------------------------------------
    sharpe = _get("Sharpe Ratio")

    # ---- Sortino Ratio (manual if not in stats) -----------------------------
    sortino = _get("Sortino Ratio")
    if np.isnan(sortino):
        try:
            returns = portfolio.returns()
            neg_returns = returns[returns < 0]
            downside_std = neg_returns.std() * np.sqrt(252)
            ann_return = (1 + returns.mean()) ** 252 - 1
            sortino = ann_return / downside_std if downside_std != 0 else np.nan
        except Exception:
            sortino = np.nan

    # ---- Max Drawdown --------------------------------------------------------
    max_dd = _get("Max Drawdown [%]")
    if np.isnan(max_dd):
        try:
            max_dd = portfolio.max_drawdown() * 100
        except Exception:
            max_dd = np.nan

    # ---- CAGR (Compound Annual Growth Rate) ----------------------------------
    cagr = _get("Annualized Return [%]")
    if np.isnan(cagr):
        try:
            total_return = portfolio.total_return()
            n_years = len(portfolio.returns()) / 252
            cagr = ((1 + total_return) ** (1 / n_years) - 1) * 100 if n_years > 0 else np.nan
        except Exception:
            cagr = np.nan

    # ---- Calmar Ratio (CAGR / |Max Drawdown|) --------------------------------
    calmar = np.nan
    if not np.isnan(cagr) and not np.isnan(max_dd) and max_dd != 0:
        calmar = (cagr / 100) / abs(max_dd / 100)

    # ---- Win Rate ------------------------------------------------------------
    win_rate = _get("Win Rate [%]")

    # ---- Total Trades --------------------------------------------------------
    total_trades = _get("Total Trades")
    if np.isnan(total_trades):
        try:
            total_trades = portfolio.trades.count()
        except Exception:
            total_trades = np.nan

    return {
        "Label":         label,
        "Sharpe":        round(sharpe,      3) if not np.isnan(sharpe)       else "N/A",
        "Sortino":       round(sortino,     3) if not np.isnan(sortino)      else "N/A",
        "MaxDD (%)":     round(max_dd,      2) if not np.isnan(max_dd)       else "N/A",
        "CAGR (%)":      round(cagr,        2) if not np.isnan(cagr)         else "N/A",
        "Calmar":        round(calmar,      3) if not np.isnan(calmar)       else "N/A",
        "Win Rate (%)":  round(win_rate,    2) if not np.isnan(win_rate)     else "N/A",
        "Total Trades":  int(total_trades)     if not np.isnan(total_trades) else "N/A",
    }


# ===========================================================================
# SECTION 6 — REPORTING
# ===========================================================================

def _print_section(title: str) -> None:
    """Print a formatted section header."""
    bar = "=" * 70
    if RICH_AVAILABLE:
        console.print(f"\n[bold cyan]{bar}[/bold cyan]")
        console.print(f"[bold yellow]  {title}[/bold yellow]")
        console.print(f"[bold cyan]{bar}[/bold cyan]")
    else:
        print(f"\n{bar}")
        print(f"  {title}")
        print(f"{bar}")


def print_results_table(
    results: List[Dict],
    title: str = "Backtest Results",
) -> None:
    """
    Display a formatted table of per-ticker performance metrics.

    Uses rich.Table if available; falls back to a plain pandas DataFrame print.

    Parameters
    ----------
    results : list of dict
        Each dict is the output of extract_metrics() for one ticker.
    title   : str
        Table heading.
    """
    _print_section(title)

    if not results:
        print("  No results to display.")
        return

    df = pd.DataFrame(results).set_index("Label")

    if RICH_AVAILABLE:
        table = Table(
            title=title,
            box=box.ROUNDED,
            header_style="bold magenta",
            show_lines=True,
        )
        table.add_column("Ticker/Label", style="bold white", no_wrap=True)
        for col in df.columns:
            table.add_column(col, justify="right")
        # ⚡ Bolt: Use itertuples() instead of iterrows() for fast read-only iteration.
        # name=None returns plain tuples instead of namedtuples: columns like
        # "MaxDD (%)" are not valid Python identifiers and would raise ValueError
        # from the namedtuple field-name validation otherwise.
        for row in df.itertuples(name=None):
            table.add_row(str(row[0]), *[str(v) for v in row[1:]])
        console.print(table)
    else:
        print(df.to_string())


def print_aggregate_metrics(results: List[Dict], title: str) -> None:
    """
    Print portfolio-level aggregate statistics (mean across tickers).

    Parameters
    ----------
    results : list of dict
    title   : str
    """
    _print_section(f"Aggregate Portfolio Metrics — {title}")

    numeric_keys = ["Sharpe", "Sortino", "MaxDD (%)", "CAGR (%)", "Calmar",
                    "Win Rate (%)", "Total Trades"]
    aggregated = {}
    for key in numeric_keys:
        vals = []
        for r in results:
            v = r.get(key, "N/A")
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        if vals:
            aggregated[key] = round(float(np.mean(vals)), 3)
        else:
            aggregated[key] = "N/A"

    if RICH_AVAILABLE:
        table = Table(box=box.SIMPLE_HEAVY, header_style="bold green")
        table.add_column("Metric",  style="bold white")
        table.add_column("Average", justify="right")
        for k, v in aggregated.items():
            table.add_row(k, str(v))
        console.print(table)
    else:
        for k, v in aggregated.items():
            print(f"  {k:<20}: {v}")


# ===========================================================================
# SECTION 7 — STRATEGY RUNNERS
# ===========================================================================

def run_ma_crossover(
    data:        Dict[str, pd.DataFrame],
    tickers:     List[str],
    fast_window: int,
    slow_window: int,
    commission:  float,
    slippage:    float,
) -> List[Dict]:
    """
    Run the Moving Average Crossover strategy for a list of tickers.

    Parameters
    ----------
    data        : dict   Ticker → OHLCV DataFrame.
    tickers     : list   Tickers to backtest (benchmark excluded here).
    fast_window : int
    slow_window : int
    commission  : float
    slippage    : float

    Returns
    -------
    list of metrics dicts (one per ticker)
    """
    _print_section(
        f"MA Crossover  |  fast={fast_window}  slow={slow_window}  "
        f"commission={commission*100:.2f}bps  slippage={slippage*100:.2f}bps"
    )

    results = []
    for ticker in tickers:
        if ticker not in data:
            print(f"  [SKIP] {ticker}: data not available.")
            continue

        close = data[ticker]["Close"].dropna()

        if len(close) < slow_window + 10:
            print(f"  [SKIP] {ticker}: insufficient history for slow_window={slow_window}.")
            continue

        entries, exits = generate_ma_crossover_signals(close, fast_window, slow_window)

        if entries.sum() == 0:
            print(f"  [SKIP] {ticker}: no entry signals generated.")
            continue

        portfolio = build_portfolio(close, entries, exits, commission, slippage)
        metrics   = extract_metrics(portfolio, label=f"{ticker} [MA]")
        results.append(metrics)
        print(f"  {ticker}: {metrics['Total Trades']} trades | "
              f"CAGR={metrics['CAGR (%)']}% | Sharpe={metrics['Sharpe']}")

    return results


def run_rsi_reversion(
    data:       Dict[str, pd.DataFrame],
    tickers:    List[str],
    rsi_period: int,
    rsi_low:    int,
    rsi_high:   int,
    commission: float,
    slippage:   float,
) -> List[Dict]:
    """
    Run the RSI Mean-Reversion strategy for a list of tickers.

    Parameters
    ----------
    data       : dict   Ticker → OHLCV DataFrame.
    tickers    : list
    rsi_period : int
    rsi_low    : int    Oversold threshold.
    rsi_high   : int    Overbought threshold.
    commission : float
    slippage   : float

    Returns
    -------
    list of metrics dicts (one per ticker)
    """
    _print_section(
        f"RSI Reversion  |  period={rsi_period}  low={rsi_low}  high={rsi_high}  "
        f"commission={commission*100:.2f}bps  slippage={slippage*100:.2f}bps"
    )

    results = []
    for ticker in tickers:
        if ticker not in data:
            print(f"  [SKIP] {ticker}: data not available.")
            continue

        close = data[ticker]["Close"].dropna()

        if len(close) < rsi_period + 10:
            print(f"  [SKIP] {ticker}: insufficient history.")
            continue

        entries, exits = generate_rsi_reversion_signals(close, rsi_period, rsi_low, rsi_high)

        if entries.sum() == 0:
            print(f"  [SKIP] {ticker}: no entry signals generated.")
            continue

        portfolio = build_portfolio(close, entries, exits, commission, slippage)
        metrics   = extract_metrics(portfolio, label=f"{ticker} [RSI]")
        results.append(metrics)
        print(f"  {ticker}: {metrics['Total Trades']} trades | "
              f"CAGR={metrics['CAGR (%)']}% | Sharpe={metrics['Sharpe']}")

    return results


def run_benchmark(
    data:       Dict[str, pd.DataFrame],
    commission: float,
) -> Optional[Dict]:
    """
    Build and report metrics for the SPY buy-and-hold benchmark.

    Parameters
    ----------
    data       : dict
    commission : float

    Returns
    -------
    dict or None
    """
    if BENCHMARK_TICKER not in data:
        print(f"  [WARN] Benchmark ticker {BENCHMARK_TICKER} not in data — skipping.")
        return None

    close     = data[BENCHMARK_TICKER]["Close"].dropna()
    portfolio = build_benchmark_portfolio(close, commission)
    metrics   = extract_metrics(portfolio, label=f"{BENCHMARK_TICKER} [B&H]")
    return metrics


# ===========================================================================
# SECTION 8 — CLI ARGUMENT PARSING
# ===========================================================================

def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.

    All parameters have sensible defaults so the script runs out of the box
    with no arguments.
    """
    parser = argparse.ArgumentParser(
        description="Vectorbt backtesting: MA Crossover & RSI Reversion",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ----- Universe ----------------------------------------------------------
    parser.add_argument(
        "--tickers", nargs="+", default=DEFAULT_TICKERS,
        metavar="TICKER",
        help="Space-separated list of ticker symbols to backtest.",
    )

    # ----- Date range --------------------------------------------------------
    parser.add_argument(
        "--start", default=DEFAULT_START,
        help="Start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end", default=DEFAULT_END,
        help="End date in YYYY-MM-DD format.",
    )

    # ----- MA Crossover params -----------------------------------------------
    parser.add_argument(
        "--fast-window", type=int, default=DEFAULT_FAST_WINDOW,
        help="Fast moving average window (MA Crossover strategy).",
    )
    parser.add_argument(
        "--slow-window", type=int, default=DEFAULT_SLOW_WINDOW,
        help="Slow moving average window (MA Crossover strategy).",
    )

    # ----- RSI params --------------------------------------------------------
    parser.add_argument(
        "--rsi-period", type=int, default=DEFAULT_RSI_PERIOD,
        help="RSI calculation period.",
    )
    parser.add_argument(
        "--rsi-low", type=int, default=DEFAULT_RSI_LOW,
        help="RSI oversold threshold (entry trigger).",
    )
    parser.add_argument(
        "--rsi-high", type=int, default=DEFAULT_RSI_HIGH,
        help="RSI overbought threshold (exit trigger).",
    )

    # ----- Transaction costs -------------------------------------------------
    parser.add_argument(
        "--commission", type=float, default=DEFAULT_COMMISSION,
        help="One-way commission as a fraction of trade value (e.g. 0.001 = 10bps).",
    )
    parser.add_argument(
        "--slippage", type=float, default=DEFAULT_SLIPPAGE,
        help="One-way slippage as a fraction of price (e.g. 0.0005 = 5bps).",
    )

    # ----- Strategy selection ------------------------------------------------
    parser.add_argument(
        "--strategy",
        choices=["ma_crossover", "rsi_reversion", "both"],
        default="both",
        help="Which strategy (or both) to run.",
    )

    return parser.parse_args()


# ===========================================================================
# SECTION 9 — MAIN ENTRY POINT
# ===========================================================================

def main() -> None:
    """
    Orchestrates the full backtesting pipeline:

    1. Parse CLI arguments.
    2. Fetch OHLCV data for all requested tickers + benchmark.
    3. Run selected strategy/strategies.
    4. Build SPY buy-and-hold benchmark for comparison.
    5. Print per-ticker results table.
    6. Print aggregate portfolio-level metrics.
    """
    args = parse_args()

    _print_section("Vectorbt Backtesting Engine")
    print(f"  Strategy  : {args.strategy}")
    print(f"  Tickers   : {args.tickers}")
    print(f"  Period    : {args.start}  →  {args.end}")
    print(f"  Commission: {args.commission * 100:.2f} bps | Slippage: {args.slippage * 100:.2f} bps")

    # ------------------------------------------------------------------
    # Step 1: Download market data
    # ------------------------------------------------------------------
    data = fetch_data(tickers=args.tickers, start=args.start, end=args.end)

    if not data:
        print("[ERROR] No data could be fetched. Check tickers and date range.")
        sys.exit(1)

    # Tickers to strategy (exclude benchmark from strategy runs — it is
    # always computed separately as buy-and-hold baseline)
    strategy_tickers = [t for t in args.tickers if t != BENCHMARK_TICKER]

    all_results: List[Dict] = []

    # ------------------------------------------------------------------
    # Step 2: Run strategy/strategies
    # ------------------------------------------------------------------
    if args.strategy in ("ma_crossover", "both"):
        ma_results = run_ma_crossover(
            data=data,
            tickers=strategy_tickers,
            fast_window=args.fast_window,
            slow_window=args.slow_window,
            commission=args.commission,
            slippage=args.slippage,
        )
        all_results.extend(ma_results)

        # Print per-strategy results table
        if ma_results:
            print_results_table(ma_results, title="MA Crossover — Per-Ticker Results")
            print_aggregate_metrics(ma_results, title="MA Crossover")

    if args.strategy in ("rsi_reversion", "both"):
        rsi_results = run_rsi_reversion(
            data=data,
            tickers=strategy_tickers,
            rsi_period=args.rsi_period,
            rsi_low=args.rsi_low,
            rsi_high=args.rsi_high,
            commission=args.commission,
            slippage=args.slippage,
        )
        all_results.extend(rsi_results)

        if rsi_results:
            print_results_table(rsi_results, title="RSI Reversion — Per-Ticker Results")
            print_aggregate_metrics(rsi_results, title="RSI Reversion")

    # ------------------------------------------------------------------
    # Step 3: Benchmark comparison
    # ------------------------------------------------------------------
    _print_section("Benchmark — SPY Buy & Hold")
    benchmark_metrics = run_benchmark(data=data, commission=args.commission)

    if benchmark_metrics:
        print(f"  SPY B&H CAGR    : {benchmark_metrics['CAGR (%)']:>8}%")
        print(f"  SPY B&H Sharpe  : {benchmark_metrics['Sharpe']:>8}")
        print(f"  SPY B&H Max DD  : {benchmark_metrics['MaxDD (%)']:>8}%")

    # ------------------------------------------------------------------
    # Step 4: Combined results table (all strategies + benchmark)
    # ------------------------------------------------------------------
    if args.strategy == "both" and all_results:
        combined = all_results.copy()
        if benchmark_metrics:
            combined.append(benchmark_metrics)
        print_results_table(combined, title="Combined Results — All Strategies vs Benchmark")

    # ------------------------------------------------------------------
    # Step 5: Overall aggregate (strategies only, excluding benchmark)
    # ------------------------------------------------------------------
    if all_results:
        print_aggregate_metrics(all_results, title="All Strategies Combined")

    _print_section("Done")


# ===========================================================================
# Entry point guard
# ===========================================================================
if __name__ == "__main__":
    main()
