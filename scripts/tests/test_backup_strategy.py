"""Tests for scripts/backup_strategy.py.

Runnable with either ``pytest scripts/tests`` or ``python scripts/tests/test_backup_strategy.py``.
Verifies extraction of the ticker list, the latest sizing ladder, budget rules,
and the 3:20 / 3:35 PM ET weekday schedule logic from an imperfect sample text.
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

Budget rules ($2 allocation policy):
- Base amount $2.
- Weekly rollover reserve max $900.
- Normal: daily cap $180, weekly cap $900.
- Elevated: daily cap $240, weekly cap $1200.
- High-volatility: daily cap $300, weekly cap $1500.
- Stress: daily cap $360, weekly cap $1800.

Schedules:
ce16ba6b - live graded DCA task, schedule 20 19 * * 1-5 UTC = 3:20 PM ET weekdays
67aa802b - post-trade slippage + Notion sync, schedule 35 19 * * 1-5 UTC = 3:35 PM ET weekdays
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


def test_extract_base_amount_recognizes_common_wordings():
    # Codex review: transcripts often phrase the base amount as "$2-base" or
    # "base $2" rather than the exact "base amount $2" the regex first
    # supported. Both extract_budgets and extract_sizing_ladder must resolve
    # these common orderings, not silently return None.
    for text in (
        "the shared $2-base ladder",
        "$2 base 0x-to-5x ladder",
        "base $2 allocation",
    ):
        assert extract_budgets(text)["base_amount_usd"] == 2.0, text
        assert extract_sizing_ladder(text)["base_amount_usd"] == 2.0, text


def test_extract_base_amount_prefers_explicit_over_superseded_incidental_mention():
    # Codex review: a messy transcript often carries a superseded "$3-base
    # ladder" mention (describing the OLD ladder) ahead of the current, more
    # deliberate "Base amount $2" declaration. re.search would return the
    # earlier $3 match; both extractors must instead prefer the explicit
    # declaration regardless of where it falls in the text.
    text = (
        "Older notes reference the $3-base ladder from before the change.\n"
        "Budget rules: Base amount $2.\n"
    )
    assert extract_budgets(text)["base_amount_usd"] == 2.0
    assert extract_sizing_ladder(text)["base_amount_usd"] == 2.0


def test_extract_base_amount_prefers_last_match_within_same_tier():
    # When only incidental "$X-base" mentions are present (no explicit "base
    # amount" declaration), or multiple explicit declarations appear, the
    # LAST one in the text wins, since later text in a running transcript is
    # more likely to reflect the current, not superseded, policy.
    incidental_only = "the old $3-base ladder is superseded by the new $2-base ladder"
    assert extract_budgets(incidental_only)["base_amount_usd"] == 2.0
    assert extract_sizing_ladder(incidental_only)["base_amount_usd"] == 2.0

    two_explicit = "Base amount $3 (superseded). Base amount $2 (current)."
    assert extract_budgets(two_explicit)["base_amount_usd"] == 2.0
    assert extract_sizing_ladder(two_explicit)["base_amount_usd"] == 2.0


def test_write_configs_refreshes_base_amount_from_dollar_dash_base_wording():
    # Codex review: without recognizing "$2-base" wording, refreshing an
    # existing $3 ladder from a transcript phrased this way left it at $3
    # (write_configs filters out the None update) despite the transcript
    # stating the new $2 policy.
    transcript = "Latest active sizing ladder, now on the $2-base ladder:\n0.1% to <1% = 1x\n"
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "sizing_ladder.json").write_text(json.dumps({"base_amount_usd": 3.0}))
        write_configs(extract_all(transcript), out, only=["ladder"])
        ladder = json.loads((out / "sizing_ladder.json").read_text())
        assert ladder["base_amount_usd"] == 2.0


def test_extract_budgets():
    b = extract_budgets(SAMPLE)
    assert b["base_amount_usd"] == 2.0
    assert b["weekly_rollover_reserve_cap_usd"] == 900.0
    tiers = {t["name"]: t for t in b["budget_tiers"]}
    assert tiers["normal"] == {"name": "normal", "daily_cap_usd": 180.0, "weekly_cap_usd": 900.0}
    assert tiers["elevated"]["daily_cap_usd"] == 240.0
    assert tiers["high_volatility"]["weekly_cap_usd"] == 1500.0
    # The stress tier carries the rollover-reserve safeguard when the transcript
    # references the weekly rollover reserve.
    assert tiers["stress"] == {
        "name": "stress",
        "daily_cap_usd": 360.0,
        "weekly_cap_usd": 1800.0,
        "requires_rollover_reserve": True,
    }


def test_write_configs_preserves_existing_tier_metadata():
    extracted = extract_all(SAMPLE)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        # Pre-existing budgets.json carries per-tier metadata that the transcript
        # does not spell out and must survive a refresh.
        (out / "budgets.json").write_text(
            json.dumps(
                {
                    "version": "keep-me",
                    "budget_tiers": [
                        {
                            "name": "stress",
                            "daily_cap_usd": 999.0,
                            "weekly_cap_usd": 999.0,
                            "requires_rollover_reserve": True,
                            "note": "Usable only to the extent prior unused rollover reserve is available.",
                            "select_when": "planned spend > $272 or 3x+ signals > 17",
                        }
                    ],
                }
            )
        )
        write_configs(extracted, out, only=["budgets"])
        budgets = json.loads((out / "budgets.json").read_text())
        assert budgets["version"] == "keep-me"  # top-level key preserved
        stress = {t["name"]: t for t in budgets["budget_tiers"]}["stress"]
        # Caps refreshed from the transcript...
        assert stress["daily_cap_usd"] == 360.0
        assert stress["weekly_cap_usd"] == 1800.0
        # ...while the safeguard, note, and selection rule are preserved.
        assert stress["requires_rollover_reserve"] is True
        assert "rollover reserve" in stress["note"]
        assert stress["select_when"].startswith("planned spend > $272")
        # The other extracted tiers are merged in too.
        assert {"normal", "elevated", "high_volatility"} <= {
            t["name"] for t in budgets["budget_tiers"]
        }


def test_schedule_weekday_logic_320_and_335_et():
    sched = extract_schedule(SAMPLE)
    by_id = {t["id"]: t for t in sched["tasks"]}

    live = by_id["ce16ba6b"]
    assert live["cron_utc"] == "20 19 * * 1-5"
    assert live["weekday_only"] is True
    assert "3:20 PM ET" in live["et_equivalent"]

    post = by_id["67aa802b"]
    assert post["cron_utc"] == "35 19 * * 1-5"
    assert post["weekday_only"] is True
    assert "3:35 PM ET" in post["et_equivalent"]

    # Live trade schedule (3:20) must be distinct from the slippage/Notion sync (3:35).
    assert live["cron_utc"] != post["cron_utc"]

    # Each parsed task appears exactly once (no duplicate-append).
    assert [t["id"] for t in sched["tasks"]].count("ce16ba6b") == 1

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
