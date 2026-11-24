"""Research one-off: past earnings announcement dates per ticker from yfinance.

Output {ticker: ["YYYY-MM-DD", ...]} plus a per-ticker status. Announcement dates are
schedule facts published ahead of time, so excluding the sessions around them in a
backtest uses no outcome data. Run: python <this> <universe tickers.json> <out.json>
"""
import json
import sys
import time

import yfinance as yf

NON_EQUITY = {"AAAU", "XLG", "QQQ", "VT", "TOPT", "QTOP", "FLKR", "DXYZ"}
universe = [t if isinstance(t, str) else t["symbol"] for t in json.load(open(sys.argv[1]))["active"]]
dates, status = {}, {}
for t in universe:
    if t in NON_EQUITY:
        continue
    try:
        df = yf.Ticker(t).get_earnings_dates(limit=24)
        got = sorted({ts.date().isoformat() for ts in (df.index if df is not None else [])})
        dates[t] = got
        status[t] = f"ok:{len(got)}" if got else "empty"
    except Exception as exc:  # research fetch: record and continue, never stop the batch
        status[t] = f"error:{type(exc).__name__}"
    time.sleep(0.4)
json.dump({"dates": dates, "status": status}, open(sys.argv[2], "w"), indent=1)
print({k: sum(1 for v in status.values() if v.startswith(k)) for k in ("ok", "empty", "error")})
