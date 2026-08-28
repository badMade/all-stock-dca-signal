"""Scratch script: compute Gate 5 candidates for the 1130ET intraday slot using
REAL fetched Robinhood market data (previous close = official settled close from
get_equity_quotes; current price = latest completed non-interpolated 5-minute bar
at/before the 11:30 ET decision anchor from get_equity_historicals) and the repo's
own signal-grading functions (multiplier_for_drop / minimum_drop_for_ticker /
select_first_allocation_base_usd / _trend_validated), rather than reimplementing
the tier logic by hand. Deleted after use.
"""
from datetime import datetime, timedelta, timezone

from intraday_dca import load_intraday_config, IntradayDCAExecutor
from near_close_dca import (
    EASTERN,
    Bar,
    select_first_allocation_base_usd,
    minimum_drop_for_ticker,
    _ticker_sizing,
)

ex = IntradayDCAExecutor(slot="1130")

decision_dt = datetime(2026, 8, 26, 11, 30, tzinfo=EASTERN)

# ticker -> (previous_close, current_price)
prices = {
"DFTX": (44.81, 44.40), "TRIN": (18.70, 18.725), "AAAU": (46.00, 45.30),
"SPCX": (137.95, 136.195), "EVR": (289.54, 292.13), "LUNR": (16.60, 16.025),
"IRDM": (47.10, 47.015), "DXYZ": (34.65, 33.82), "LITE": (885.565, 913.33),
"ASML": (1744.16, 1741.34), "DRAM": (56.24, 56.425), "STX": (821.67, 845.4102),
"MSFT": (491.71, 495.005), "AAPL": (309.90, 312.5399), "AMAT": (480.04, 479.635),
"AMD": (479.18, 484.31), "CBRS": (183.915, 180.145), "MRVL": (240.38, 240.8254),
"BTX": (8.86, 8.910), "STM": (49.46, 49.375), "SNDK": (1480.77, 1500.260),
"WDC": (450.75, 466.390), "TSM": (417.41, 417.200), "AVGO": (356.74, 351.560),
"NVDA": (213.05, 211.1001), "QBTS": (19.35, 17.7599), "FLKR": (59.43, 59.250),
"INTC": (87.48, 86.575), "GOOGL": (346.96, 342.820), "GOOG": (343.34, 339.520),
"AMZN": (261.06, 260.166), "GLW": (147.16, 149.235), "MA": (599.37, 596.600),
"GS": (1058.88, 1043.510), "MU": (932.97, 941.9118), "NFLX": (82.23, 81.820),
"XLG": (62.10, 61.995), "TOPT": (33.32, 33.230), "QTOP": (36.69, 36.6701),
"SATA": (100.00, 100.00), "RGTI": (16.94, 16.130), "IBM": (234.19, 230.130),
"GFUZ": (7.14, 7.160), "TXN": (260.05, 262.070), "RKLB": (66.91, 66.290),
"GE": (349.54, 354.720), "VRT": (255.75, 262.9299), "CAT": (811.29, 821.805),
"QQQ": (710.72, 710.470), "QCOM": (160.56, 160.965), "ABBV": (265.79, 261.0002),
"JPM": (356.69, 356.1288), "VT": (160.99, 160.885), "RVII": (22.69, 22.200),
"RVI": (27.12, 27.010), "VCX": (44.44, 42.640), "TRLV": (11.32, 11.630),
}

results = []
for ticker, (prev_close, current_price) in prices.items():
    if current_price >= prev_close:
        continue  # not below prior close -> no buy, skip entirely
    drop_fraction = (prev_close - current_price) / prev_close
    multiplier = ex.multiplier_for_drop(drop_fraction, ticker)
    eff_min_drop = (
        minimum_drop_for_ticker(ex.sizing_config, ticker)
        if _ticker_sizing(ticker, ex.sizing_config)
        else ex.sizing_config.minimum_drop_vs_previous_close
    )
    if drop_fraction < eff_min_drop or multiplier <= 0:
        continue
    base_usd, base_reason = select_first_allocation_base_usd(
        current_price, prev_close, 0.0, ex._sizing_config if hasattr(ex, "_sizing_config") else ex.sizing_config, ticker
    )
    results.append((ticker, drop_fraction * 100, multiplier, base_usd, base_reason, prev_close, current_price))

results.sort(key=lambda r: -r[1])
print(f"{'ticker':6} {'drop%':>7} {'mult':>5} {'base':>5} {'amt':>6}  reason")
total = 0.0
for ticker, drop_pct, mult, base, reason, pc, cp in results:
    amt = round(base * mult, 2)
    total += amt
    print(f"{ticker:6} {drop_pct:7.3f} {mult:5.1f} {base:5.2f} {amt:6.2f}  {reason}")
print(f"\n{len(results)} qualifying candidates, total (pre-budget/pre-topup) ${total:.2f}")
