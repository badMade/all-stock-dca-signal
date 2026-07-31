"""Tests for scripts/foreground_primary.py and scripts/close_window_signal.py.

Runnable with ``pytest scripts/tests`` or
``python scripts/tests/test_foreground_primary.py``.

Covers the Jul 30 2026 close-of-day validator patches:
  * every recognised foreground primary marker is detected, on the top level and
    in the nested blocks a run_state / run_summary / manifest entry uses
  * a marker on a prior-day artifact never activates today's foreground mode
  * missing scheduled evidence WITH foreground primary active does not escalate
  * missing scheduled evidence WITHOUT foreground primary active still escalates
  * real failures escalate even when foreground primary mode is active
  * the read-only what-would-have-fired close-window summary, including the 3x
    reporting cap and the mode comparison columns
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Repo root too: the scripts modules under test import the shared top-level
# dca_fileio helper, which direct invocation cannot resolve from scripts/ alone.
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

from close_window_signal import (  # noqa: E402
    LABEL_CAPPED,
    REASON_DROP_BELOW_FIRST_TIER,
    REASON_MISSING_DECISION_PRICE,
    REASON_NOT_BELOW_PRIOR_CLOSE,
    REPORTED_MULTIPLIER_CAP,
    STATUS_NO_SIGNAL,
    STATUS_SIGNAL_MODE_NOT_ACTIVE,
    STATUS_UNKNOWN_MISSING_DATA,
    STATUS_WOULD_HAVE_FIRED,
    build_close_window_summary,
    reported_multiplier,
)
from foreground_primary import (  # noqa: E402
    FINDING_BROKER_MISMATCH,
    FINDING_SAFETY_FAILURE,
    FOREGROUND_PRIMARY_MARKER,
    REASON_NO_ARTIFACTS,
    REASON_UNREADABLE,
    REASON_WRONG_DAY,
    detect_foreground_primary_context,
    evaluate_scheduled_evidence_alert,
    find_foreground_primary_markers,
)

TODAY = "2026-07-30"
YESTERDAY = "2026-07-29"

# The live ladder shape from config/sizing_ladder.json, as (fraction, multiplier).
LADDER = (
    (0.001, 1.0),
    (0.01, 1.5),
    (0.02, 2.0),
    (0.03, 2.5),
    (0.04, 3.0),
    (0.05, 3.5),
    (0.06, 4.0),
    (0.07, 4.5),
    (0.08, 5.0),
)


# --------------------------------------------------------------------------
# Marker detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"trading_day": TODAY, "foreground_primary_surrogate_context": True},
        {"trading_day": TODAY, "valid_primary_executor_evidence": True},
        {"trading_day": TODAY, "run_origin": "foreground_primary_surrogate"},
        {"trading_day": TODAY, "run_origin": "manual_foreground_primary"},
        {"trading_day": TODAY, "markers": ["foreground_primary_surrogate_context"]},
        {"trading_day": TODAY, "context_kind": "foreground_primary_surrogate_context"},
        {
            "trading_day": TODAY,
            "run_classification": {"run_origin": "manual_foreground_primary"},
        },
        {
            "trading_day": TODAY,
            "run_summary": {"foreground_primary_surrogate_context": True},
        },
    ],
    ids=[
        "boolean_flag",
        "primary_executor_evidence",
        "run_origin_surrogate",
        "run_origin_manual_foreground",
        "marker_string_in_list",
        "marker_string_as_value",
        "nested_run_classification_origin",
        "nested_run_summary_flag",
    ],
)
def test_every_documented_marker_activates_foreground_primary_mode(payload):
    context = detect_foreground_primary_context([payload], trading_day=TODAY)
    assert context.active is True
    assert context.markers


def test_valid_foreground_primary_context_preserves_evidence_separation():
    """The whole point of the patch: context, never scheduled evidence."""
    context = detect_foreground_primary_context(
        [{"trading_day": TODAY, "foreground_primary_surrogate_context": True}],
        trading_day=TODAY,
    )
    assert context.active is True
    assert context.valid_scheduled_evidence is False
    assert context.valid_afternoon_scheduled_evidence is False
    assert context.top_up_context_only is True

    report = context.to_report()
    assert report["foreground_primary_mode_active"] is True
    assert report["valid_scheduled_evidence"] is False
    assert report["valid_afternoon_scheduled_evidence"] is False
    assert report["top_up_context_only"] is True
    assert report["classification"] == FOREGROUND_PRIMARY_MARKER


def test_foreground_artifact_paths_are_listed_for_the_report(tmp_path):
    artifact = tmp_path / "run_state.json"
    artifact.write_text(
        json.dumps({"trading_day": TODAY, "run_origin": "foreground_primary_surrogate"}),
        encoding="utf-8",
    )
    context = detect_foreground_primary_context([artifact], trading_day=TODAY)
    assert context.active is True
    assert context.artifact_paths == [str(artifact)]


def test_prior_day_marker_does_not_activate_todays_foreground_mode():
    """A stale marker must not silence today's alert forever."""
    context = detect_foreground_primary_context(
        [{"trading_day": YESTERDAY, "foreground_primary_surrogate_context": True}],
        trading_day=TODAY,
    )
    assert context.active is False
    assert (("<inline>", REASON_WRONG_DAY)) in context.rejected


def test_marker_without_a_trading_day_is_rejected():
    context = detect_foreground_primary_context(
        [{"foreground_primary_surrogate_context": True}], trading_day=TODAY
    )
    assert context.active is False


def test_marker_mentioned_inside_prose_is_not_a_declaration():
    """Substring matching would let a note that denies the mode switch it on."""
    payload = {
        "trading_day": TODAY,
        "notes": "this run is NOT foreground_primary_surrogate_context, it is scheduled",
    }
    assert find_foreground_primary_markers(payload) == []
    assert detect_foreground_primary_context([payload], trading_day=TODAY).active is False


def test_false_and_stringy_flags_are_not_declarations():
    for value in (False, "true", "false", 1, 0, None):
        payload = {"trading_day": TODAY, FOREGROUND_PRIMARY_MARKER: value}
        assert detect_foreground_primary_context([payload], trading_day=TODAY).active is False


def test_unreadable_artifact_is_recorded_and_does_not_raise(tmp_path):
    broken = tmp_path / "run_summary.json"
    broken.write_text("{not json", encoding="utf-8")
    good = {"trading_day": TODAY, "valid_primary_executor_evidence": True}
    context = detect_foreground_primary_context([broken, good], trading_day=TODAY)
    assert context.active is True
    assert (str(broken), REASON_UNREADABLE) in context.rejected


def test_no_artifacts_supplied_is_not_foreground_primary_mode():
    context = detect_foreground_primary_context(None, trading_day=TODAY)
    assert context.active is False
    assert ("<none>", REASON_NO_ARTIFACTS) in context.rejected


# --------------------------------------------------------------------------
# Escalation gating
# --------------------------------------------------------------------------


def test_missing_scheduled_evidence_with_foreground_primary_active_does_not_escalate():
    foreground = detect_foreground_primary_context(
        [{"trading_day": TODAY, "foreground_primary_surrogate_context": True}],
        trading_day=TODAY,
    )
    alert = evaluate_scheduled_evidence_alert(
        scheduled_evidence_ok=False,
        scheduled_evidence_reasons=["no scheduled run_state at .../run_state.json"],
        foreground=foreground,
    )
    assert alert.escalate is False
    assert alert.foreground_primary_mode_active is True
    assert alert.suppressed_reasons == ["no scheduled run_state at .../run_state.json"]
    assert alert.escalation_reasons == []


def test_missing_scheduled_evidence_without_foreground_primary_active_escalates():
    foreground = detect_foreground_primary_context([], trading_day=TODAY)
    alert = evaluate_scheduled_evidence_alert(
        scheduled_evidence_ok=False,
        scheduled_evidence_reasons=["run_state is not a valid scheduled run"],
        foreground=foreground,
    )
    assert alert.escalate is True
    assert alert.foreground_primary_mode_active is False
    assert alert.escalation_reasons == ["run_state is not a valid scheduled run"]
    assert alert.suppressed_reasons == []


def test_missing_scheduled_evidence_escalates_when_no_context_is_passed_at_all():
    alert = evaluate_scheduled_evidence_alert(scheduled_evidence_ok=False)
    assert alert.escalate is True
    assert alert.foreground_primary_mode_active is False


def test_real_failures_still_escalate_under_foreground_primary_mode():
    foreground = detect_foreground_primary_context(
        [{"trading_day": TODAY, "valid_primary_executor_evidence": True}],
        trading_day=TODAY,
    )
    alert = evaluate_scheduled_evidence_alert(
        scheduled_evidence_ok=False,
        scheduled_evidence_reasons=["no scheduled run_state"],
        foreground=foreground,
        blocking_findings=[FINDING_SAFETY_FAILURE, FINDING_BROKER_MISMATCH],
    )
    assert alert.escalate is True
    assert alert.foreground_primary_mode_active is True
    assert FINDING_SAFETY_FAILURE in alert.escalation_reasons
    assert FINDING_BROKER_MISMATCH in alert.escalation_reasons
    # The scheduled-evidence finding is still the only suppressible one.
    assert alert.suppressed_reasons == ["no scheduled run_state"]


def test_valid_scheduled_evidence_escalates_nothing():
    alert = evaluate_scheduled_evidence_alert(scheduled_evidence_ok=True)
    assert alert.escalate is False
    assert alert.suppressed_reasons == []


# --------------------------------------------------------------------------
# Close-window what-would-have-fired summary
# --------------------------------------------------------------------------


def test_close_window_summary_grades_the_watchlist_and_compares_modes():
    summary = build_close_window_summary(
        {
            "NVDA": {"decision_price": 98.0, "prior_close": 100.0, "data_source": "finance"},
            "MSFT": {"decision_price": 101.0, "prior_close": 100.0, "data_source": "finance"},
            "GFUZ": {"decision_price": 99.999, "prior_close": 100.0, "data_source": "robinhood"},
            "LUNR": {"prior_close": 100.0, "data_source": "yfinance_fallback"},
        },
        tiers=LADDER,
        trading_day=TODAY,
        watchlist=["NVDA", "MSFT", "GFUZ", "LUNR"],
        background_mode_active=False,
        foreground_primary_mode_active=True,
    )

    assert summary["analytics_only"] is True
    assert summary["places_reviews_cancels_sells_or_modifies_orders"] is False
    assert summary["watchlist_count"] == 4
    assert summary["would_have_fired_tickers"] == ["NVDA"]

    rows = {row["ticker"]: row for row in summary["rows"]}
    assert set(rows) == {"NVDA", "MSFT", "GFUZ", "LUNR"}
    for row in summary["rows"]:
        assert set(row) >= {
            "ticker",
            "decision_price",
            "prior_close",
            "drop_pct",
            "ladder_multiplier_capped_at_3x",
            "background_mode_status",
            "foreground_mode_status",
            "reason_if_not_fired",
            "data_source",
        }

    # A 2% drop fires: 2.0x on the ladder, and on this day only in foreground mode.
    nvda = rows["NVDA"]
    assert nvda["drop_pct"] == pytest.approx(2.0)
    assert nvda["ladder_multiplier_capped_at_3x"] == 2.0
    assert nvda["reason_if_not_fired"] is None
    assert nvda["foreground_mode_status"] == STATUS_WOULD_HAVE_FIRED
    assert nvda["background_mode_status"] == STATUS_SIGNAL_MODE_NOT_ACTIVE
    assert nvda["data_source"] == "finance"

    # Above the prior close: no signal in either mode.
    assert rows["MSFT"]["reason_if_not_fired"] == REASON_NOT_BELOW_PRIOR_CLOSE
    assert rows["MSFT"]["background_mode_status"] == STATUS_NO_SIGNAL
    assert rows["MSFT"]["foreground_mode_status"] == STATUS_NO_SIGNAL

    # Below the close but under the 0.1% first tier.
    assert rows["GFUZ"]["reason_if_not_fired"] == REASON_DROP_BELOW_FIRST_TIER
    assert rows["GFUZ"]["ladder_multiplier_capped_at_3x"] == 0.0

    # A watchlist ticker whose quote failed is reported, not silently dropped.
    assert rows["LUNR"]["reason_if_not_fired"] == REASON_MISSING_DECISION_PRICE
    assert rows["LUNR"]["background_mode_status"] == STATUS_UNKNOWN_MISSING_DATA
    assert rows["LUNR"]["foreground_mode_status"] == STATUS_UNKNOWN_MISSING_DATA


def test_close_window_summary_caps_the_reported_multiplier_at_3x():
    # A 9% drop is 5x on the live ladder; the summary reports 3x plus a label.
    summary = build_close_window_summary(
        {"RGTI": {"decision_price": 91.0, "prior_close": 100.0, "data_source": "finance"}},
        tiers=LADDER,
        trading_day=TODAY,
        background_mode_active=True,
        foreground_primary_mode_active=False,
    )
    row = summary["rows"][0]
    assert row["ladder_multiplier_capped_at_3x"] == REPORTED_MULTIPLIER_CAP == 3.0
    assert row["ladder_multiplier_label"] == LABEL_CAPPED
    assert row["background_mode_status"] == STATUS_WOULD_HAVE_FIRED
    assert row["foreground_mode_status"] == STATUS_SIGNAL_MODE_NOT_ACTIVE


def test_reported_multiplier_leaves_the_0x_to_3x_buckets_untouched():
    for raw in (0.0, 1.0, 1.5, 2.0, 2.5, 3.0):
        assert reported_multiplier(raw) == (raw, None)
    assert reported_multiplier(3.5) == (3.0, LABEL_CAPPED)
    assert reported_multiplier(5.0) == (3.0, LABEL_CAPPED)


def test_close_window_summary_matches_the_live_ladder_loaded_from_config():
    """The summary must grade with the real ladder, not a copy that can drift."""
    from dca_config import load_sizing_tiers

    tiers = load_sizing_tiers(Path(__file__).resolve().parents[2] / "config")["sizing_tiers"]
    summary = build_close_window_summary(
        {"AMD": {"decision_price": 95.0, "prior_close": 100.0, "data_source": "finance"}},
        tiers=tiers,
        trading_day=TODAY,
    )
    # A 5% drop is 3.5x on the live ladder, reported as the 3x cap.
    row = summary["rows"][0]
    assert row["drop_pct"] == pytest.approx(5.0)
    assert row["ladder_multiplier_capped_at_3x"] == 3.0
    assert row["ladder_multiplier_label"] == LABEL_CAPPED


def test_close_window_summary_is_pure_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    build_close_window_summary(
        {"NVDA": {"decision_price": 98.0, "prior_close": 100.0, "data_source": "finance"}},
        tiers=LADDER,
        trading_day=TODAY,
    )
    assert set(tmp_path.iterdir()) == before


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
