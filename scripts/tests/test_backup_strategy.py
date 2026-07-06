"""Tests for scripts/backup_strategy.py.

Runnable with either ``pytest scripts/tests`` or ``python scripts/tests/test_backup_strategy.py``.
Verifies extraction of the ticker list, the latest sizing ladder, budget rules,
and the 3:30 / 3:45 PM ET weekday schedule logic from an imperfect sample text.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backup_strategy import (  # noqa: E402
    extract_all,
    extract_budgets,
    extract_schedule,
    extract_sizing_ladder,
    extract_tickers,
    write_configs,
)


SAMPLE = """
Notes from the DCA thread (messy paste).

Current active ticker universe:
DFTX, TRIN, AAAU, SPCX, TRLV, RVI, VCX, EVR, LUNR, IRDM, DXYZ, LITE, ASML,
DRAM, STX, MSFT, AAPL, AMAT, AMD, CBRS, MRVL, BTX, STM, SNDK, WDC, TSM, AVGO,
NVDA, QBTS, FLKR, INTC, GOOGL, GOOG, AMZN, GLW, MA, GS, MU, NFLX, XLG, TOPT,
QTOP, SATA, RGTI, IBM.
Excluded: BYDDY
Corrected symbols: IRDMI -> IRDM, GLWR -> GLW.

Latest active sizing ladder (older 0.1%-12% ladders are superseded):
drop <0.1% = 0x
0.1% to <1% = 1x
1% to <2% = 1.5x
2% to <3% = 2x
3% to <4% = 2.5x
4% to <5% = 3x
5% to <6% = 3.5x
6% to <7% = 4x
7% to <8% = 4.5x
>=8% = 5x, no upper cutoff

Budget/reserve rules:
- Base amount $1.
- Daily base cap $60.
- Carry-forward reserve max $420.
- Hard daily max $120.
- Weekly hard cap $600.

Schedules:
ce16ba6b - live graded DCA task, schedule 30 19 * * 1-5 UTC = 3:30 PM ET weekdays
67aa802b - post-trade slippage + Notion sync, schedule 45 19 * * 1-5 UTC = 3:45 PM ET weekdays
041d9d90 - weekly dashboard, schedule 0 21 * * 5 UTC = Fridays 5:00 PM ET
"""


def test_extract_tickers_active_count_and_membership():
    t = extract_tickers(SAMPLE)
    assert len(t["active"]) == 45
    assert "NVDA" in t["active"]
    assert "IBM" in t["active"]
    # Excluded symbol must not leak into active.
    assert "BYDDY" not in t["active"]
    assert t["excluded"] == ["BYDDY"]
    assert t["symbol_corrections"] == {"IRDMI": "IRDM", "GLWR": "GLW"}


def test_extract_latest_ladder():
    ladder = extract_sizing_ladder(SAMPLE)
    tiers = ladder["tiers"]
    assert ladder["no_buy_below_pct"] == 0.1
    # Nine buy tiers (the 0x no-buy line is not a tier).
    assert len(tiers) == 9
    # First tier: 0.1% to <1% = 1x.
    assert tiers[0] == {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 1.0}
    # Top tier: >=8% = 5x with no upper cutoff.
    assert tiers[-1]["min_drop_pct"] == 8.0
    assert tiers[-1]["max_drop_pct"] is None
    # Max multiplier is 5x (confirms the 12x-era ladder is not present).
    assert max(tier["multiplier"] for tier in tiers) == 5.0


def test_extract_budgets():
    b = extract_budgets(SAMPLE)
    assert b["base_amount_usd"] == 1.0
    assert b["daily_base_cap_usd"] == 60.0
    assert b["carry_forward_reserve_cap_usd"] == 420.0
    assert b["hard_daily_max_usd"] == 120.0
    assert b["weekly_hard_cap_usd"] == 600.0


def test_schedule_weekday_logic_330_and_345_et():
    sched = extract_schedule(SAMPLE)
    by_id = {t["id"]: t for t in sched["tasks"]}

    live = by_id["ce16ba6b"]
    assert live["cron_utc"] == "30 19 * * 1-5"
    assert live["weekday_only"] is True
    assert "3:30 PM ET" in live["et_equivalent"]

    post = by_id["67aa802b"]
    assert post["cron_utc"] == "45 19 * * 1-5"
    assert post["weekday_only"] is True
    assert "3:45 PM ET" in post["et_equivalent"]

    # Live trade schedule (3:30) must be distinct from the slippage/Notion sync (3:45).
    assert live["cron_utc"] != post["cron_utc"]

    weekly = by_id["041d9d90"]
    assert weekly["weekday_only"] is False  # Fridays only, not 1-5


def test_write_configs_merges_and_persists():
    extracted = extract_all(SAMPLE)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        # Pre-existing file with an extra key that must be preserved on merge.
        (out / "tickers.json").write_text(json.dumps({"version": "keep-me"}))
        written = write_configs(extracted, out, only=None)
        assert "tickers.json" in written
        assert "sizing_ladder.json" in written

        tickers = json.loads((out / "tickers.json").read_text())
        assert tickers["version"] == "keep-me"  # preserved
        assert len(tickers["active"]) == 45      # merged in

        ladder = json.loads((out / "sizing_ladder.json").read_text())
        assert len(ladder["tiers"]) == 9


def test_robust_to_imperfect_transcript():
    # Missing sections must not raise; extractors return empty structures.
    messy = "totally unrelated text with no tickers or ladders"
    result = extract_all(messy)
    assert result["tickers"]["active"] == []
    assert result["sizing_ladder"]["tiers"] == []
    assert result["budgets"] == {}
    assert result["schedule"]["tasks"] == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
