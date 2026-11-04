"""The Gate 9 finalizer posts the public slot-status signal (operator decision 2026-11-03).

2026-11-02: the signal never updated on a live trading day. Only ``local_heartbeat start``/``finish`` posted it, and
no live-mirror prompt calls either. Every live prompt does call ``python3 -m scripts.finalize_run_evidence`` on every
outcome, so the finalizer's CLI posts the slot's status after a run it accepted.

The operator's rule: a run that finished as designed counts as ``succeeded``, whether it placed orders, found nothing
to buy, ran out of budget or stopped on a safety check. Only a real error or abort counts as ``failed``, which is what
lets a backup act. A dry run, or a run the finalizer refused, posts nothing: a refused call may not be this slot's real
fire. Posting is best-effort and never changes the finalizer's exit code or its printed result.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_finalize_run_evidence import gate5_doc, put  # noqa: E402

from scripts import finalize_run_evidence as fre  # noqa: E402
from scripts import slot_status  # noqa: E402
from scripts.verify_schedule import slot_run_dir_relative  # noqa: E402

DAY = "2026-09-22"
BUDGET = '{"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900}'
ORDERS = '[{"ticker": "MA", "order_id": "ord-1", "submitted_amount_usd": 2.5, "status": "filled"}]'


@pytest.fixture(autouse=True)
def synthetic_account(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")


@pytest.fixture()
def posted(monkeypatch):
    calls = []

    def fake_update(day, slot, status, *, now=None, publisher="configured"):
        calls.append((day, slot, status))
        return {"recorded": True, "published": True, "detail": "test"}

    monkeypatch.setattr(slot_status, "update", fake_update)
    return calls


def _argv(tmp_path, status, *, slot="1230", started="2026-09-22T12:31:00-04:00", outcome="END_SILENTLY", extra=()):
    argv = ["--slot", slot, "--started-at", started, "--root", str(tmp_path), "--outcome", outcome,
            "--order-submit-status", status, *extra]
    if status == "submitted":
        argv += ["--orders-json", str(_file(tmp_path, "orders.json", ORDERS)),
                 "--budget-json", str(_file(tmp_path, "budget.json", BUDGET))]
    return argv


def _file(tmp_path, name, text):
    path = tmp_path / "inputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _gate5_document(tmp_path, slot="1230"):
    """A real generator document (the main test file's own helper), so zero candidates is a quiet tape."""
    put(tmp_path, slot, "gate5_document.json", gate5_doc(slot, no_data_count=40))


# -- what counts as succeeded ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status, outcome", [
    ("submitted", "ORDERS_PLACED"),
    ("no_orders_submitted", "END_SILENTLY"),  # a safety check said do not buy: a deliberate stop
    ("no_orders_submitted", "NO_ORDERS_GATE6_5_ZEROED"),
    ("not_submitted_budget_exhausted", "NO_ORDERS"),
])
def test_a_run_that_finished_as_designed_posts_succeeded(tmp_path, posted, status, outcome):
    assert fre.main(_argv(tmp_path, status, outcome=outcome)) in (0, 1)
    assert posted == [(DAY, "1230", "succeeded")]


def test_a_quiet_tape_with_no_candidates_posts_succeeded(tmp_path, posted):
    _gate5_document(tmp_path)
    assert fre.main(_argv(tmp_path, "not_submitted_no_candidates", outcome="NO_CANDIDATES")) in (0, 1)
    assert posted == [(DAY, "1230", "succeeded")]


# -- what counts as failed ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [
    "not_submitted_error",
    "blocked_placement_phase_unreachable",
    "not_submitted_confirmation_required",  # no order went in: a backup may still have to act
])
def test_a_real_error_or_abort_posts_failed(tmp_path, posted, status):
    extra = ("--error-class", "market_data_unavailable", "--error-detail", "provider returned nothing")
    fre.main(_argv(tmp_path, status, outcome="ABORTED", extra=extra))
    assert posted == [(DAY, "1230", "failed")]


def test_every_live_order_submit_status_has_a_signal(tmp_path):
    # A status added later must be given a signal on purpose, never fall through to "nothing posted".
    assert set(fre.SLOT_SIGNAL_FOR_ORDER_SUBMIT_STATUS) == set(fre.ORDER_SUBMIT_STATUSES)
    assert set(fre.SLOT_SIGNAL_FOR_ORDER_SUBMIT_STATUS.values()) == {"succeeded", "failed"}


# -- which day and slot -------------------------------------------------------------------------------------------


def test_the_slot_and_trading_day_come_from_the_run_in_eastern_time(tmp_path, posted):
    # 1230ET is the same slot as 1230; a UTC start instant still lands on the ET trading day.
    assert fre.main(_argv(tmp_path, "no_orders_submitted", slot="1230ET", started="2026-09-22T16:31:00Z")) == 0
    assert posted == [(DAY, "1230", "succeeded")]


# -- when nothing is posted ---------------------------------------------------------------------------------------


def test_a_refused_off_window_call_posts_nothing(tmp_path, posted):
    assert fre.main(_argv(tmp_path, "no_orders_submitted", started="2026-09-22T09:00:00-04:00")) == 2
    assert posted == []


def test_an_outage_dressed_as_a_quiet_tape_is_refused_and_posts_nothing(tmp_path, posted):
    run_dir = tmp_path / slot_run_dir_relative("1230", date.fromisoformat(DAY))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "candidates.json").write_text('{"candidates": []}', encoding="utf-8")
    assert fre.main(_argv(tmp_path, "no_orders_submitted")) == 2
    assert posted == []


def test_a_dry_run_posts_nothing(tmp_path, posted):
    fre.main(_argv(tmp_path, "no_orders_submitted", extra=("--dry-run",)))
    assert posted == []


# -- best-effort: posting never changes the finalizer ----------------------------------------------------------------


def test_a_posting_failure_never_changes_the_exit_code_or_the_printed_result(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("git push failed: https://x-access-token:secret@github.com/...")

    monkeypatch.setattr(slot_status, "update", lambda *a, **k: {"recorded": True, "published": True})
    assert fre.main(_argv(tmp_path / "a", "no_orders_submitted")) == 0
    quiet = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(slot_status, "update", boom)
    assert fre.main(_argv(tmp_path / "b", "no_orders_submitted")) == 0
    out = capsys.readouterr()
    loud = json.loads(out.out)
    assert set(loud) == set(quiet)  # same result keys; nothing about the signal is added to stdout
    assert "secret" not in out.out + out.err


def test_the_suite_never_pushes(tmp_path, monkeypatch):
    # conftest.py sets DCA_DISABLE_SLOT_STATUS_PUBLISH, so the real update records locally and publishes nothing.
    import os

    assert os.environ.get(slot_status.DISABLE_ENV)
    monkeypatch.setattr(slot_status, "state_dir", lambda: tmp_path / "slot_status")
    assert fre.main(_argv(tmp_path, "no_orders_submitted")) == 0
    document = json.loads(slot_status.state_path().read_text(encoding="utf-8"))
    assert document["trading_day"] == DAY and document["slots"]["1230"]["status"] == "succeeded"
