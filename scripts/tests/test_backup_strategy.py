"""Tests for scripts/backup_strategy.py.

Runnable with either ``pytest scripts/tests`` or ``python scripts/tests/test_backup_strategy.py``.
Verifies extraction of the ticker list, the latest sizing ladder, budget rules,
and the 3:20 / 3:35 PM ET weekday schedule logic from an imperfect sample text.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from scripts.backup_strategy import (  # noqa: E402
        extract_all,
        extract_budgets,
        extract_schedule,
        extract_sizing_ladder,
        extract_tickers,
        main,
        write_configs,
    )
except ImportError:  # pragma: no cover - fallback when running directly in scripts/
    from backup_strategy import (  # type: ignore # noqa: E402
        extract_all,
        extract_budgets,
        extract_schedule,
        extract_sizing_ladder,
        extract_tickers,
        main,
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


def test_extract_tickers_recognizes_whole_share_membership():
    """Codex review ("Teach transcript backups about whole-share
    membership"): a "Whole-share:" marker must be extracted into its own
    category -- separate from `excluded` -- and dropped from `active`, the
    same way `excluded` already is, so this transcript-based restore path
    can actually reproduce tickers.json's third universe category.
    """
    text = """
    Current active ticker universe:
    AAPL, MSFT, BYDDY, NVDA.
    Whole-share: BYDDY
    """
    t = extract_tickers(text)
    assert t["whole_share"] == ["BYDDY"]
    assert "BYDDY" not in t["active"]
    assert t["active"] == ["AAPL", "MSFT", "NVDA"]
    assert t["excluded"] == []


def test_extract_tickers_rejects_a_symbol_listed_in_both_excluded_and_whole_share():
    """Codex review ("Reject overlap between restored ticker categories"):
    a transcript explicitly listing the same symbol in both "Excluded:" and
    "Whole-share:" is genuinely contradictory -- unlike the active-list
    overlap resolved just above (where excluded/whole_share always win),
    there is no correct symbol to silently drop here. Without this check,
    write_configs would persist an overlapping partition straight into
    tickers.json, which dca_config.load_strategy_config then rejects on its
    very next load, preventing the strategy from starting.
    """
    text = """
    Current active ticker universe:
    AAPL, MSFT.
    Excluded: BYDDY
    Whole-share: BYDDY
    """
    with pytest.raises(ValueError, match="both 'Excluded:' and 'Whole-share:'"):
        extract_tickers(text)


def test_extract_tickers_whole_share_applies_symbol_corrections():
    text = """
    Current active ticker universe:
    AAPL, MSFT.
    Whole-share: BYDY
    Corrected symbols: BYDY -> BYDDY.
    """
    t = extract_tickers(text)
    assert t["whole_share"] == ["BYDDY"]


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
        ladder = json.loads((out / "sizing_ladder.json").read_bytes())
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
        budgets = json.loads((out / "budgets.json").read_bytes())
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

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["version"] == "keep-me"  # preserved
        assert len(tickers["active"]) == 45      # merged in

        ladder = json.loads((out / "sizing_ladder.json").read_bytes())
        assert len(ladder["tiers"]) == 9


def test_write_configs_clears_a_stale_whole_share_array_when_explicitly_emptied():
    """Codex review ("Allow transcript restores to clear whole-share
    membership"): a transcript that EXPLICITLY says there is no longer any
    whole-share ticker (e.g. "Whole-share: (none)" after the last one moved
    back into `active`) must be able to CLEAR an existing tickers.json's
    stale `whole_share` array, not leave it untouched. _merge_into_file's
    generic empty-value filter otherwise treats extract_tickers's explicit
    `"whole_share": []` the same as "the extractor found nothing to say
    about this key," silently preserving a symbol that could now overlap
    the newly-written `active` list and violate load_tickers's
    disjointness invariant.

    Refined by "Preserve unmentioned ticker categories during transcript
    restores" (see the sibling test below): the marker itself ("Whole-share:")
    must be present for this clearing behavior to apply -- a transcript that
    never mentions the category at all must NOT clear it (see
    test_write_configs_preserves_an_unmentioned_whole_share_array below).
    """
    text = """
    Current active ticker universe:
    AAPL, MSFT.
    Whole-share: (none)
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["whole_share"] == []
    assert extracted["tickers"]["whole_share_mentioned"] is True
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "tickers.json").write_text(json.dumps({"whole_share": ["BYDDY"]}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["whole_share"] == []


def test_write_configs_clears_a_stale_excluded_array_when_explicitly_emptied():
    """Same gap as the whole_share case above, for the `excluded` category --
    both are ticker-partition keys where an EXPLICITLY mentioned but empty
    list is a deliberate "nothing in this category now" signal, distinct
    from the category never being mentioned at all.
    """
    text = """
    Current active ticker universe:
    AAPL, MSFT, NVDA, GOOGL, AMZN.
    Excluded: (none)
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["excluded"] == []
    assert extracted["tickers"]["excluded_mentioned"] is True
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "tickers.json").write_text(json.dumps({"excluded": ["OLDTICKER"]}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["excluded"] == []


def test_extract_tickers_treats_bare_none_as_an_empty_whole_share_marker():
    """Codex review ("Treat NONE as an empty category marker"): unlike the
    parenthesized "(none)" example elsewhere in this module (whose parens
    already fail the uppercase-symbol regex), a BARE "Whole-share: NONE"
    uppercases to "NONE", which DOES pass the same `[A-Z.]{1,6}` regex a
    real ticker would -- without treating it as a stopword, extract_tickers
    returns "NONE" as an actual extracted symbol and write_configs would
    persist it into tickers.json instead of clearing the category.
    """
    text = """
    Current active ticker universe:
    AAPL, MSFT.
    Whole-share: NONE
    Excluded: NONE
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["whole_share"] == []
    assert extracted["tickers"]["whole_share_mentioned"] is True
    assert extracted["tickers"]["excluded"] == []
    assert extracted["tickers"]["excluded_mentioned"] is True

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "tickers.json").write_text(json.dumps({"whole_share": ["BYDDY"], "excluded": ["OLDTICKER"]}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["whole_share"] == []
        assert tickers["excluded"] == []


def test_write_configs_clears_whole_share_from_a_category_only_transcript():
    """Codex review ("Allow category-only transcript restores"): a
    transcript containing ONLY a category update -- no "Current active
    ticker universe" section at all -- is a supported partial-transcript
    case. write_configs's outer condition previously required a nonempty
    extracted `active` list before merging tickers.json at all, so this
    category-only transcript would write NOTHING despite whole_share_mentioned
    correctly identifying a deliberate clear, leaving the stale
    `whole_share` array in place.
    """
    text = """
    Whole-share: (none)
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["active"] == []
    assert extracted["tickers"]["whole_share_mentioned"] is True
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "tickers.json").write_text(json.dumps({"active": ["AAPL", "MSFT"], "whole_share": ["BYDDY"]}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        # whole_share is cleared, per the explicit marker...
        assert tickers["whole_share"] == []
        # ...but active (never mentioned in this transcript) is left
        # completely untouched, not wiped out by the empty extracted list.
        assert tickers["active"] == ["AAPL", "MSFT"]


def test_write_configs_removes_a_moved_symbol_from_its_old_category():
    """Codex review ("Reconcile retained categories during partial ticker
    restores"): a category-only transcript ("Whole-share: AAPL") merges
    that new whole_share list in, but if AAPL is still sitting in the
    EXISTING, retained (unmentioned) `active` list, the merge previously
    left it there untouched -- writing AAPL into BOTH categories at once
    and violating the disjointness invariant load_strategy_config enforces,
    which would then reject the config and prevent the strategy from
    starting.
    """
    text = """
    Whole-share: AAPL
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["whole_share"] == ["AAPL"]
    assert extracted["tickers"]["whole_share_mentioned"] is True
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "tickers.json").write_text(json.dumps({"active": ["AAPL", "MSFT"], "excluded": []}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["whole_share"] == ["AAPL"]
        # AAPL moved OUT of active -- MSFT, untouched, remains.
        assert tickers["active"] == ["MSFT"]


def test_write_configs_preserves_an_unmentioned_whole_share_array():
    """Codex review ("Preserve unmentioned ticker categories during
    transcript restores"): a transcript that contains an active-universe
    block but simply OMITS any "Whole-share:" section entirely (an
    abbreviated/older transcript, not one that explicitly clears the
    category) must leave an existing tickers.json's `whole_share` array
    completely untouched -- not overwrite it with extract_tickers's default
    empty list, which would silently remove a real whole-share ticker
    (e.g. BYDDY) the transcript never asked to change at all.
    """
    extracted = extract_all(SAMPLE)  # SAMPLE has no "Whole-share:" marker at all
    assert extracted["tickers"]["whole_share"] == []
    assert extracted["tickers"]["whole_share_mentioned"] is False
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        # SAMPLE explicitly mentions "Excluded: BYDDY", so BYDDY itself is not
        # a safe probe symbol here -- it would legitimately be stripped from
        # whole_share by the retained-category reconciliation this file also
        # tests (see test_write_configs_removes_a_moved_symbol_from_its_old_category).
        # Use a symbol SAMPLE never mentions at all, to isolate this test's
        # actual concern: an omitted "Whole-share:" section leaves the
        # existing array untouched.
        (out / "tickers.json").write_text(json.dumps({"whole_share": ["ZAZZ"]}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["whole_share"] == ["ZAZZ"]


def test_write_configs_preserves_an_unmentioned_excluded_array():
    """Same gap as the whole_share case above, for the `excluded` category."""
    text = """
    Current active ticker universe:
    AAPL, MSFT, NVDA, GOOGL, AMZN.
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["excluded"] == []
    assert extracted["tickers"]["excluded_mentioned"] is False
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "tickers.json").write_text(json.dumps({"excluded": ["OLDTICKER"]}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["excluded"] == ["OLDTICKER"]


def test_write_configs_preserves_whole_share_despite_descriptive_prose_mentioning_it():
    """Codex review ("Require explicit category markers during transcript
    restore"): the earlier "_mentioned" detection matched ANY occurrence of
    "whole-share"/"excluded" followed by whitespace, not just an actual
    "Whole-share:"/"Excluded:" section marker. Ordinary descriptive prose
    like "BYDDY is a whole-share ticker" has a space right after "share",
    which matched too -- setting whole_share_mentioned=True with an empty
    extracted list (the symbol-extraction regex requires an uppercase run
    right after the marker, which "ticker" isn't, so nothing is actually
    extracted), and write_configs would then overwrite an existing
    whole_share membership with [] even though the transcript never
    intended to clear anything. Detection must require the literal colon.
    """
    text = """
    Current active ticker universe:
    AAPL, MSFT, NVDA.
    BYDDY is a whole-share ticker with no fractional buys, excluded from
    the normal dollar-amount ladder.
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["whole_share"] == []
    assert extracted["tickers"]["whole_share_mentioned"] is False
    assert extracted["tickers"]["excluded"] == []
    assert extracted["tickers"]["excluded_mentioned"] is False
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "tickers.json").write_text(json.dumps({"whole_share": ["BYDDY"], "excluded": ["OLDTICKER"]}))
        write_configs(extracted, out, only=["tickers"])

        tickers = json.loads((out / "tickers.json").read_bytes())
        assert tickers["whole_share"] == ["BYDDY"]
        assert tickers["excluded"] == ["OLDTICKER"]


def test_extract_tickers_requires_a_colon_for_whole_share_and_excluded_extraction():
    """Codex review ("Require a colon when extracting whole-share symbols"):
    the SYMBOL-EXTRACTION regex (not just the *_mentioned detection fixed
    above) still accepted bare whitespace instead of a literal colon. If a
    transcript contains a REAL "Whole-share:" section (setting
    whole_share_mentioned=True) plus separate, unrelated prose like "the
    whole-share XYZ is special" elsewhere, the extraction regex matched
    THAT prose too and added "XYZ" to the extracted whole_share list --
    write_configs would then persist this unintended symbol into
    tickers.json, where it could enter live strategy evaluation. Same class
    of bug applies to "excluded" extraction. Both must require the literal
    colon, matching the *_mentioned checks' own anchor.
    """
    text = """
    Current active ticker universe:
    AAPL, MSFT, NVDA.

    Whole-share:
    BYDDY

    the whole-share XYZ is special, and the excluded FOO is unrelated too.
    """
    extracted = extract_all(text)
    assert extracted["tickers"]["whole_share"] == ["BYDDY"]
    assert extracted["tickers"]["whole_share_mentioned"] is True
    assert "XYZ" not in extracted["tickers"]["whole_share"]
    assert extracted["tickers"]["excluded"] == []
    assert "FOO" not in extracted["tickers"]["excluded"]


def test_robust_to_imperfect_transcript():
    # Missing sections must not raise; extractors return empty structures.
    messy = "totally unrelated text with no tickers or ladders"
    result = extract_all(messy)
    assert result["tickers"]["active"] == []
    assert result["sizing_ladder"]["tiers"] == []
    assert result["budgets"] == {}
    assert result["schedule"]["tasks"] == []


# --- CLI: main() ---------------------------------------------------------------
#
# Every case above exercises extract_all()/write_configs() directly; nothing
# exercised the CLI entrypoint (--transcript / --write / --only / missing-file
# exit code) that a human or agent actually runs.


def test_main_missing_transcript_exits_two(tmp_path, capsys):
    rc = main(["--transcript", str(tmp_path / "does-not-exist.md")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_main_without_write_only_prints_and_does_not_touch_disk(tmp_path, capsys):
    transcript = tmp_path / "transcript.md"
    transcript.write_text(SAMPLE, encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = main(["--transcript", str(transcript), "--out-dir", str(out_dir)])

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert len(printed["tickers"]["active"]) == 45
    assert not out_dir.exists()


def test_main_with_write_persists_config_files(tmp_path, capsys):
    transcript = tmp_path / "transcript.md"
    transcript.write_text(SAMPLE, encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = main(["--transcript", str(transcript), "--out-dir", str(out_dir), "--write"])

    assert rc == 0
    stderr = capsys.readouterr().err
    assert "wrote/updated" in stderr
    assert (out_dir / "tickers.json").exists()
    assert (out_dir / "sizing_ladder.json").exists()


def test_main_write_with_only_restricts_which_files_are_written(tmp_path, capsys):
    transcript = tmp_path / "transcript.md"
    transcript.write_text(SAMPLE, encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = main(
        ["--transcript", str(transcript), "--out-dir", str(out_dir), "--write", "--only", "tickers"]
    )

    assert rc == 0
    assert (out_dir / "tickers.json").exists()
    assert not (out_dir / "sizing_ladder.json").exists()


def test_main_write_with_nothing_extracted_reports_nothing_written(tmp_path, capsys):
    transcript = tmp_path / "transcript.md"
    transcript.write_text("totally unrelated text with no tickers or ladders", encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = main(["--transcript", str(transcript), "--out-dir", str(out_dir), "--write"])

    assert rc == 0
    assert "nothing extracted to write" in capsys.readouterr().err


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
