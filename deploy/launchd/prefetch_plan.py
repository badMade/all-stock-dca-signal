#!/usr/bin/env python3
"""Plan one prefetch_slot.sh run: resolve the slot and print what its Gate 5 generator scores.

Prints ``slot<TAB>root<TAB>HH:MM<TAB>interval<TAB>comma-tickers`` on success.
Exit 3: outside config/trading_window.json. 4: not a trading day for this
slot (closed, or an early close ends it). 5: ``auto`` with no slot due.
2: bad slot argument. Reads DAY, NOW_ET and SLOT_ARG from the environment.
Kept out of the shell script because bash 3.2 (macOS /bin/bash) misparses
an apostrophe inside a heredoc nested in $(...).
"""
import logging
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

# Run as a file from deploy/launchd/, so the checkout's modules are not on
# sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# The executors log an account-pinning warning on construction when the
# account env var is unset; this script never touches a broker, so it is noise
# in the launchd log.
logging.disable(logging.WARNING)

import dca_config  # noqa: E402
import dca_paths  # noqa: E402
import dca_runner  # noqa: E402

SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430", "1530")
day = date.fromisoformat(os.environ["DAY"])
arg = os.environ["SLOT_ARG"].upper().removesuffix("ET")


def plan(slot):
    """(decision time, bar interval minutes, tickers) exactly as that slot's Gate 5 generator scores them."""
    config_dir = dca_config.get_config_dir()
    if slot == "1530":
        # scripts/generate_buy_candidates.py: Finance-primary 15-minute bars at
        # 15:30; whole-share tickers are handled outside Gate 5 and GFUZ is
        # Robinhood-routed, so neither needs a yfinance row.
        t = dca_config.load_tickers(config_dir)
        if not t.get("tickers"):
            raise SystemExit("tickers.json has no active tickers")
        skip = set(t.get("excluded_tickers", ())) | set(t.get("whole_share_tickers", ())) | {"GFUZ"}
        return time(15, 30), 15, [s for s in t["tickers"] if s not in skip]
    if slot == "0935":
        from scripts.generate_market_open_candidates import MARKET_OPEN_SPEC as spec
    elif slot == "1030":
        from scripts.generate_morning_candidates import MORNING_SPEC as spec
    elif slot == "1230":
        from scripts.generate_midday_candidates import MIDDAY_SPEC as spec
    else:
        from scripts.generate_intraday_candidates import INTRADAY_SPECS

        spec = INTRADAY_SPECS[slot]
    # The same loader and narrowing scripts/stream_candidates.py's run_cli
    # applies: configured universe minus excluded, and minus any ticker the
    # policy routes to another source (GFUZ on 12:30), which the fallback
    # never scores.
    config = spec.config_loader(config_dir, allow_unpinned_account=True)
    executor = spec.executor_cls(config=config, mode=spec.live_mode, **spec.executor_kwargs)
    interval = spec.source_interval_minutes or config.interval_minutes
    tickers = [
        s for s in config.tickers
        if s not in config.excluded_tickers and s not in spec.ticker_source_overrides
    ]
    return executor.decision_time, interval, tickers


if arg == "AUTO":
    # launchd fires one minute after each decision time; accept a late wake
    # (sleep, coalesced runs) for up to 14 minutes, then give up -- a payload
    # fetched after the session has already run Gate 5 helps nobody.
    now = datetime.combine(day, time.fromisoformat(os.environ["NOW_ET"]))
    due = []
    for slot in SLOTS:
        decision = datetime.combine(day, time(int(slot[:2]), int(slot[2:])))
        if decision + timedelta(minutes=1) <= now < decision + timedelta(minutes=15):
            due.append(slot)
    if not due:
        raise SystemExit(5)
    slot = due[-1]
elif arg in SLOTS:
    slot = arg
else:
    print(f"unknown slot {arg!r}; expected one of {', '.join(SLOTS)} or auto", file=sys.stderr)
    raise SystemExit(2)

status = dca_runner.market_day_status(day)
decision, interval, tickers = plan(slot)
# A 1:00 PM early close ends every slot at or after 13:00; the earlier ones
# still run on those days.
if status.is_closed or (status.is_early_close and decision >= time(13, 0)):
    raise SystemExit(4)
approved = dca_config.load_approved_dates(dca_config.get_config_dir()).get("approved_test_dates")
if not approved or day not in approved:
    raise SystemExit(3)
if not tickers:
    raise SystemExit(f"slot {slot} has no tickers to fetch")
# Its own tree, NOT the slot's run directory: creating <stream>/<day>_<slot>ET
# before the session fires would turn a missed slot's no_evidence into
# evidence_incomplete in scripts/verify_schedule.py, and a run directory is
# what several same-day scans read as "this slot ran".
root = dca_paths.stream_root("yfinance_prefetch", day.isoformat())
print("\t".join([slot, root, decision.strftime("%H:%M"), str(interval), ",".join(tickers)]))
