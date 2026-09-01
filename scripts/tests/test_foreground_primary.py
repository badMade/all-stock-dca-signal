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

try:
    from scripts.close_window_signal import (  # noqa: E402
        LABEL_CAPPED,
        REASON_DROP_BELOW_FIRST_TIER,
        REASON_MISSING_DECISION_PRICE,
        REASON_MISSING_LOOKBACK_PRICE,
        REASON_NOT_BELOW_PRIOR_CLOSE,
        REASON_TREND_NOT_DOWN,
        REPORTED_MULTIPLIER_CAP,
        STATUS_NO_SIGNAL,
        STATUS_SIGNAL_MODE_NOT_ACTIVE,
        STATUS_UNKNOWN_MISSING_DATA,
        STATUS_WOULD_HAVE_FIRED,
        build_close_window_summary,
        reported_multiplier,
    )
    from scripts.foreground_primary import (  # noqa: E402
        FINDING_BROKER_MISMATCH,
        FINDING_FOREGROUND_DRIFT,
        FINDING_SAFETY_FAILURE,
        FOREGROUND_PRIMARY_MARKER,
        REASON_NO_ARTIFACTS,
        REASON_POINTER_STALE,
        REASON_POINTER_UNKNOWN_STREAM,
        REASON_UNREADABLE,
        REASON_WRONG_DAY,
        detect_foreground_primary_context,
        evaluate_scheduled_evidence_alert,
        find_foreground_primary_markers,
        reconcile_foreground_orders,
    )
except ModuleNotFoundError as _import_exc:  # pragma: no cover - fallback when running directly in scripts/
    # Only a failure to resolve the "scripts" package itself -- e.g. running
    # this file directly as `python scripts/tests/test_foreground_primary.py`
    # without the repo root on sys.path -- means package-style import
    # doesn't apply here and the flat, run-from-scripts/ import style is
    # needed instead. A ModuleNotFoundError for anything else (a real
    # missing dependency inside either module's OWN imports) must not be
    # silently reinterpreted as "package layout doesn't resolve" and
    # retried through a duplicate module instance under a different import
    # path, which would obscure the real failure and could test against a
    # second, separately-initialized copy of the module (Codex review, PR
    # #1166: "Narrow the remaining direct-import fallbacks"; AGENTS.md: "Do
    # not wrap imports in broad try/except blocks").
    if _import_exc.name not in (
        "scripts",
        "scripts.close_window_signal",
        "scripts.foreground_primary",
    ):
        raise
    from close_window_signal import (  # type: ignore # noqa: E402
        LABEL_CAPPED,
        REASON_DROP_BELOW_FIRST_TIER,
        REASON_MISSING_DECISION_PRICE,
        REASON_MISSING_LOOKBACK_PRICE,
        REASON_NOT_BELOW_PRIOR_CLOSE,
        REASON_TREND_NOT_DOWN,
        REPORTED_MULTIPLIER_CAP,
        STATUS_NO_SIGNAL,
        STATUS_SIGNAL_MODE_NOT_ACTIVE,
        STATUS_UNKNOWN_MISSING_DATA,
        STATUS_WOULD_HAVE_FIRED,
        build_close_window_summary,
        reported_multiplier,
    )
    from foreground_primary import (  # type: ignore # noqa: E402
        FINDING_BROKER_MISMATCH,
        FINDING_FOREGROUND_DRIFT,
        FINDING_SAFETY_FAILURE,
        FOREGROUND_PRIMARY_MARKER,
        REASON_NO_ARTIFACTS,
        REASON_POINTER_STALE,
        REASON_POINTER_UNKNOWN_STREAM,
        REASON_UNREADABLE,
        REASON_WRONG_DAY,
        detect_foreground_primary_context,
        evaluate_scheduled_evidence_alert,
        find_foreground_primary_markers,
        reconcile_foreground_orders,
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
# Same-day latest-pointer validation
# --------------------------------------------------------------------------


def _pointer_target(tmp_path, trading_day=TODAY):
    target = tmp_path / "morning_dca" / "run_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"trading_day": trading_day}), encoding="utf-8")
    return target


def _pointer_payload(target, *, stream="morning", pointer_status=None):
    payload = {
        "trading_day": TODAY,
        FOREGROUND_PRIMARY_MARKER: True,
        "run_id": "morning_live_20260730_1030ET",
        "run_state_path": str(target),
        "stream": stream,
    }
    if pointer_status is not None:
        payload["pointer_status"] = pointer_status
    return payload


def test_pointer_status_valid_recovery_context_is_accepted(tmp_path):
    target = _pointer_target(tmp_path)
    pointer = _pointer_payload(target, pointer_status="valid_recovery_context")
    context = detect_foreground_primary_context(
        [pointer], trading_day=TODAY, expected_roots={"morning": str(tmp_path / "morning_dca")}
    )
    assert context.active is True
    assert context.rejected == []


def test_pointer_status_stale_is_still_rejected(tmp_path):
    target = _pointer_target(tmp_path)
    pointer = _pointer_payload(target, pointer_status="stale")
    context = detect_foreground_primary_context(
        [pointer], trading_day=TODAY, expected_roots={"morning": str(tmp_path / "morning_dca")}
    )
    assert context.active is False
    assert context.rejected[0][1] == REASON_POINTER_STALE


def test_pointer_with_stream_absent_from_expected_roots_is_rejected(tmp_path):
    target = _pointer_target(tmp_path)
    pointer = _pointer_payload(target, stream="not_a_real_stream")
    context = detect_foreground_primary_context(
        [pointer], trading_day=TODAY, expected_roots={"morning": str(tmp_path / "morning_dca")}
    )
    assert context.active is False
    assert context.rejected[0][1] == REASON_POINTER_UNKNOWN_STREAM


def test_pointer_stream_check_is_skipped_when_no_expected_roots_supplied(tmp_path):
    target = _pointer_target(tmp_path)
    pointer = _pointer_payload(target, stream="not_a_real_stream")
    context = detect_foreground_primary_context([pointer], trading_day=TODAY)
    assert context.active is True
    assert context.rejected == []


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
            "NVDA": {
                "decision_price": 98.0,
                "prior_close": 100.0,
                "lookback_price": 99.0,
                "data_source": "finance",
            },
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


def test_close_window_summary_treats_missing_lookback_price_as_unknown_not_fired():
    """A ticker that clears the drop/tier checks but has no lookback_price cannot be
    trend-validated, so it must not be reported as would_have_fired (Codex review:
    "Require trend data before reporting a fired signal")."""
    summary = build_close_window_summary(
        {"NVDA": {"decision_price": 98.0, "prior_close": 100.0, "data_source": "finance"}},
        tiers=LADDER,
        trading_day=TODAY,
        background_mode_active=True,
        foreground_primary_mode_active=True,
    )
    row = summary["rows"][0]
    assert row["reason_if_not_fired"] == REASON_MISSING_LOOKBACK_PRICE
    assert row["background_mode_status"] == STATUS_UNKNOWN_MISSING_DATA
    assert row["foreground_mode_status"] == STATUS_UNKNOWN_MISSING_DATA
    assert summary["would_have_fired_tickers"] == []


def test_close_window_summary_trend_gate_blocks_fired_verdict_when_price_above_lookback():
    """lookback_price present but the trend isn't down: REASON_TREND_NOT_DOWN, not fired."""
    summary = build_close_window_summary(
        {
            "NVDA": {
                "decision_price": 98.0,
                "prior_close": 100.0,
                "lookback_price": 97.0,
                "data_source": "finance",
            }
        },
        tiers=LADDER,
        trading_day=TODAY,
    )
    row = summary["rows"][0]
    assert row["reason_if_not_fired"] == REASON_TREND_NOT_DOWN
    assert row["background_mode_status"] == STATUS_NO_SIGNAL


def test_close_window_summary_caps_the_reported_multiplier_at_3x():
    # A 9% drop is 5x on the live ladder; the summary reports 3x plus a label.
    summary = build_close_window_summary(
        {
            "RGTI": {
                "decision_price": 91.0,
                "prior_close": 100.0,
                "lookback_price": 92.0,
                "data_source": "finance",
            }
        },
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


def test_close_window_summary_grades_a_calibrated_ticker_with_its_own_ladder():
    """A ticker present in per_ticker_tiers is graded with its own calibrated
    ladder, not the shared global tiers (Codex review: "Grade each ticker
    with its active per-ticker ladder"). A modest LUNR decline clears the
    lenient global first tier (0.1%) but stays below LUNR's stricter
    calibrated first tier (5%), so it must not be reported as fired."""
    quotes = {
        "LUNR": {
            "decision_price": 98.0,
            "prior_close": 100.0,
            "lookback_price": 99.0,
            "data_source": "finance",
        },
    }
    per_ticker_tiers = {"LUNR": ((0.05, 1.0), (0.08, 2.0))}

    global_only = build_close_window_summary(
        quotes, tiers=LADDER, trading_day=TODAY,
    )
    assert global_only["would_have_fired_tickers"] == ["LUNR"]

    calibrated = build_close_window_summary(
        quotes, tiers=LADDER, trading_day=TODAY, per_ticker_tiers=per_ticker_tiers,
    )
    assert calibrated["would_have_fired_tickers"] == []
    row = calibrated["rows"][0]
    assert row["reason_if_not_fired"] == REASON_DROP_BELOW_FIRST_TIER

    # A ticker absent from per_ticker_tiers still falls back to the shared ladder.
    quotes["NVDA"] = quotes.pop("LUNR")
    fallback = build_close_window_summary(
        quotes, tiers=LADDER, trading_day=TODAY, per_ticker_tiers=per_ticker_tiers,
    )
    assert fallback["would_have_fired_tickers"] == ["NVDA"]


def test_close_window_summary_matches_the_live_ladder_loaded_from_config():
    """The summary must grade with the real ladder, not a copy that can drift."""
    from dca_config import load_sizing_tiers

    tiers = load_sizing_tiers(Path(__file__).resolve().parents[2] / "config")["sizing_tiers"]
    summary = build_close_window_summary(
        {"AMD": {"decision_price": 93.5, "prior_close": 100.0, "data_source": "finance"}},
        tiers=tiers,
        trading_day=TODAY,
    )
    # A 6.5% drop is 3.5x on the live ladder (config/sizing_ladder.json's
    # 2026-08-02 rescale), reported as the 3x cap.
    row = summary["rows"][0]
    assert row["drop_pct"] == pytest.approx(6.5)
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


def test_reconcile_foreground_orders_passes_when_the_single_order_id_matches():
    artifact = {"buy_candidates": [{"ticker": "AAPL", "order_id": "RH-1"}]}
    assert reconcile_foreground_orders([artifact], fills=[{"order_id": "RH-1"}]) == []


def test_reconcile_foreground_orders_flags_a_mismatched_single_order_id():
    artifact = {"buy_candidates": [{"ticker": "AAPL", "order_id": "RH-GHOST"}]}
    assert reconcile_foreground_orders([artifact], fills=[{"order_id": "RH-1"}]) == [FINDING_FOREGROUND_DRIFT]


def test_reconcile_foreground_orders_checks_every_aggregated_whole_share_id():
    """Codex review ("Reconcile every ID in aggregated whole-share
    outcomes"): a row aggregating two recovered whole-share claims (see
    near_close_dca._merge_whole_share_submission_outcomes) keeps "order_id"
    as a single scalar -- the NEWER of the two ids -- while
    "whole_share_order_ids" carries every distinct id the row actually
    represents. Checking only the scalar "order_id" can pass while an
    OLDER aggregated id is absent from the broker's own fill records,
    hiding a real discrepancy.
    """
    artifact = {
        "buy_candidates": [
            {
                "ticker": "BYDDY",
                "order_id": "RH-NEWER",
                "whole_share_order_ids": ["RH-OLDER", "RH-NEWER"],
            }
        ]
    }
    # The scalar order_id matches broker fills, but the older aggregated id
    # does not -- this must still be flagged as drift.
    assert reconcile_foreground_orders(
        [artifact], fills=[{"order_id": "RH-NEWER"}]
    ) == [FINDING_FOREGROUND_DRIFT]
    # With BOTH aggregated ids backed by broker fills, no drift.
    assert reconcile_foreground_orders(
        [artifact], fills=[{"order_id": "RH-NEWER"}, {"order_id": "RH-OLDER"}]
    ) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
