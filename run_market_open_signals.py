"""Run the repo's real market-open (0935ET) signal generation using statically
fetched Robinhood MCP data (get_equity_historicals) captured moments ago.

This is a data-adapter shim, not a reimplementation: it builds Bar objects from
the raw historicals responses and feeds them into the actual
MarketOpenDCAExecutor.evaluate_signal() code path (trend validation exempt for
this slot per config/market_open_dca.json), exactly mirroring what
dca_live_adapters.RobinhoodDataProvider does for a live session.
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from near_close_dca import Bar, EASTERN  # noqa: E402
from market_open_dca import MarketOpenDCAExecutor, load_market_open_config  # noqa: E402

DATA_PATH = "market_open_raw_data.json"

with open(DATA_PATH) as fh:
    raw = json.load(fh)


def _parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class StaticProvider:
    source = None

    def __init__(self, daily_bars, minute_bars):
        self.daily_bars = daily_bars
        self.minute_bars = minute_bars

    def get_previous_close_bar(self, ticker, as_of):
        bars = self.daily_bars.get(ticker, [])
        as_of_date = as_of.astimezone(EASTERN).date()
        candidates = [b for b in bars if b.timestamp.astimezone(EASTERN).date() < as_of_date]
        if not candidates:
            return None
        return max(candidates, key=lambda b: b.timestamp)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        bars = self.minute_bars.get(ticker, [])
        return [b for b in bars if start <= b.timestamp <= end]


def _bars_from_payload(payload_bars):
    out = []
    for b in payload_bars:
        out.append(
            Bar(
                timestamp=_parse_ts(b["begins_at"]),
                open=float(b["open_price"]),
                high=float(b["high_price"]),
                low=float(b["low_price"]),
                close=float(b["close_price"]),
                volume=float(b.get("volume") or 0),
            )
        )
    return out


daily_bars = {t: _bars_from_payload(v) for t, v in raw["daily"].items()}
minute_bars = {t: _bars_from_payload(v) for t, v in raw["minute"].items()}

provider = StaticProvider(daily_bars, minute_bars)

config = load_market_open_config(allow_unpinned_account=True)
executor = MarketOpenDCAExecutor(config=config)

now = datetime.now(tz=timezone.utc).astimezone(EASTERN)

results = []
for ticker in sorted(set(daily_bars) | set(minute_bars)):
    sig = executor.evaluate_signal(ticker, now, provider)
    results.append(
        {
            "ticker": ticker,
            "action": sig.action.value if hasattr(sig.action, "value") else str(sig.action),
            "current_price": sig.current_price,
            "previous_close": sig.previous_close,
            "previous_low": sig.previous_low,
            "multiplier": sig.multiplier,
            "hypothetical_amount_usd": sig.hypothetical_amount_usd,
            "reason": sig.reason,
            "warnings": list(sig.warnings),
        }
    )

with open("market_open_signal_results.json", "w") as fh:
    json.dump(results, fh, indent=2)

buys = [r for r in results if r["action"] == "buy" and r["multiplier"] > 0]
print(f"Total evaluated: {len(results)}")
print(f"BUY candidates: {len(buys)}")
for r in sorted(buys, key=lambda r: -r["multiplier"]):
    print(
        f"  {r['ticker']:6s} mult={r['multiplier']:.2f} "
        f"px={r['current_price']:.4f} prev_close={r['previous_close']:.4f} "
        f"prev_low={r['previous_low']} amt=${r['hypothetical_amount_usd']:.2f}  {r['reason']}"
    )
no_data = [r for r in results if r["action"] == "no_data"]
if no_data:
    print(f"NO_DATA: {[r['ticker'] for r in no_data]}")
