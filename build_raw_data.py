"""Build market_open_raw_data.json from the previous-close/low and 9:30-9:35 ET
decision-window values captured via live get_equity_historicals calls this run.

previous close/low = the 2026-08-20 daily bar (last completed regular session
before today). decision bar = the 2026-08-21 13:34:00Z (9:34 AM ET) one-minute
bar, the last bar completed at/before the 9:35 AM ET decision anchor used by
MarketOpenDCAExecutor (decision_time=09:35 -> decision_dt=13:35:00Z; the bar
labeled 13:34 covers 13:34-13:35 and is the latest completed bar at that anchor).
"""
import json
import os
from pathlib import Path


def main(output_dir: str | Path | None = None) -> None:
    # ticker -> (prev_close, prev_low) from the 2026-08-20 daily bar
    PREV = {
        "DFTX": (45.40, 44.20), "TRIN": (18.27, 18.07), "AAAU": (44.65, 44.06),
        "SPCX": (134.00, 130.39), "EVR": (287.09, 286.77), "LUNR": (17.93, 17.322),
        "IRDM": (48.45, 47.90), "DXYZ": (34.65, 31.80), "LITE": (879.28, 815.03),
        "ASML": (1750.31, 1741.02),
        "DRAM": (57.58, 55.895), "STX": (850.24, 827.60), "MSFT": (481.15, 479.50),
        "AAPL": (311.30, 310.65), "AMAT": (496.21, 487.4701), "AMD": (469.455, 460.27),
        "CBRS": (209.85, 202.88), "MRVL": (251.01, 234.527), "BTX": (8.99, 8.81),
        "STM": (49.92, 49.675),
        "SNDK": (1600.62, 1554.00), "WDC": (469.05, 453.70), "TSM": (416.00, 407.7183),
        "AVGO": (364.03, 362.2501), "NVDA": (216.85, 215.66), "QBTS": (18.80, 18.44),
        "FLKR": (58.60, 57.85), "INTC": (92.13, 89.85), "GOOGL": (340.67, 338.5651),
        "GOOG": (338.20, 336.17),
        "AMZN": (260.11, 259.52), "GLW": (151.45, 149.25), "MA": (573.85, 571.80),
        "GS": (1001.95, 1001.68), "MU": (974.33, 929.07), "NFLX": (80.14, 79.585),
        "XLG": (61.90, 61.86), "TOPT": (33.26, 33.23), "QTOP": (36.7692, 36.65),
        "SATA": (99.91, 99.735),
        "RGTI": (16.065, 15.665), "IBM": (233.69, 233.03), "GFUZ": (6.935, 6.75),
        "TXN": (265.60, 264.76), "RKLB": (72.95, 71.75), "GE": (344.64, 343.56),
        "VRT": (264.63, 252.16), "CAT": (815.39, 801.00), "QQQ": (710.93, 708.52),
        "QCOM": (160.74, 159.41),
        "ABBV": (261.83, 260.4812), "JPM": (351.55, 351.55), "VT": (159.93, 159.805),
        "RVII": (23.41, 23.10), "RVI": (27.00, 26.70), "VCX": (39.42, 37.30),
        "TRLV": (10.25, 9.80),
    }

    # ticker -> close of the 2026-08-21T13:30:00Z one-minute bar (9:30 AM ET,
    # the first completed bar of the session -- this is the bar
    # completed_bar_anchor(decision_dt=13:35Z, interval_minutes=5, ...) resolves to
    # (decision_dt - 5min = 13:30Z) given assume_bar_timestamps_are_start_times.
    DECISION_CLOSE = {
        "DFTX": 45.870, "TRIN": 18.350, "AAAU": 45.175, "SPCX": 134.670,
        "EVR": 291.350, "LUNR": 18.755, "IRDM": 49.000, "DXYZ": 34.305,
        "LITE": 899.000, "ASML": 1765.310,
        "DRAM": 58.530, "STX": 860.040, "MSFT": 479.710, "AAPL": 311.330,
        "AMAT": 500.680, "AMD": 475.750, "CBRS": 208.070, "MRVL": 251.200,
        "BTX": 9.090, "STM": 50.590,
        "SNDK": 1605.4175, "WDC": 476.110, "TSM": 423.720, "AVGO": 370.100,
        "NVDA": 217.850, "QBTS": 19.350, "FLKR": 58.595, "INTC": 92.135,
        "GOOGL": 341.190, "GOOG": 338.710,
        "AMZN": 259.370, "GLW": 154.260, "MA": 575.160, "GS": 1017.500,
        "MU": 983.210, "NFLX": 79.8618, "XLG": 62.095, "TOPT": 33.370,
        "QTOP": 36.970, "SATA": 100.000,
        "RGTI": 16.465, "IBM": 233.6999, "GFUZ": 6.970, "TXN": 267.190,
        "RKLB": 73.735, "GE": 347.630, "VRT": 266.365, "CAT": 829.485,
        "QQQ": 714.090, "QCOM": 162.7799,
        "ABBV": 260.520, "JPM": 353.5275, "VT": 160.875, "RVII": 23.355,
        "RVI": 26.880, "VCX": 39.900, "TRLV": 10.300,
    }

    daily = {}
    minute = {}
    for t, (pc, pl) in PREV.items():
        daily[t] = [
            {
                "begins_at": "2026-08-20T00:00:00Z",
                "open_price": str(pc), "close_price": str(pc),
                "high_price": str(max(pc, pl)), "low_price": str(pl),
                "volume": 0, "session": "reg",
            }
        ]
        dc = DECISION_CLOSE[t]
        minute[t] = [
            {
                "begins_at": "2026-08-21T13:30:00Z",
                "open_price": str(dc), "close_price": str(dc),
                "high_price": str(dc), "low_price": str(dc),
                "volume": 0, "session": "reg",
            }
        ]

    out_dir = Path(output_dir) if output_dir else Path(os.getcwd())
    out_file = out_dir / "market_open_raw_data.json"

    with open(out_file, "w") as fh:
        json.dump({"daily": daily, "minute": minute}, fh, indent=2)

    print("wrote market_open_raw_data.json with", len(daily), "tickers")


if __name__ == "__main__":
    main()
