"""Tests for scripts/evidence_publish.py: the durable-evidence half of a cloud run.

Written first, from the behaviour the plan specifies, before any implementation.
A cloud container is thrown away after every fire, so Gate 9 writes the evidence
locally (as it always has) and then this command publishes it. Its rules:

* the local artifacts are never moved or deleted, whatever the push does;
* a failed push leaves a marker, keeps the local commit, and returns an alert
  text carrying the key run numbers, so the operator can still see what traded;
* nothing is ever pushed that contains the account number, exact or shaped;
* ``claim`` is a compare-and-swap on the remote: a second executor for the same
  slot and day is refused, and an unreachable store refuses too (fail closed).

Contract under test. Exit codes: 0 ok, 3 push failed, 4 refused (secret),
5 already claimed, 6 store unreachable. Stdout is one JSON object.

    main(["publish", "--evidence-root", ROOT, "--remote", URL, "--trading-day", D,
          "--slot", "1330", "--code-sha", SHA, "--max-attempts", "2",
          "--backoff-seconds", "0"], env=ENV) -> int
          -> {"status": "published" | "push_failed" | "refused_secret",
              "alert_text": str, "refused_paths": [relative paths]}

    main(["claim", "--evidence-root", ROOT, "--remote", URL, "--trading-day", D,
          "--slot", "1330", "--claimant", "run-A"], env=ENV) -> int
          -> {"status": "claimed" | "already_claimed" | "store_unreachable",
              "claimed_by": str}

The evidence remote's ``main`` carries ``MANIFEST.json``:
``{"version": 1, "days": {"<day>": {"slots": [...]}}, "last_publish": {"day", "slot", "code_sha"}}``.
"""

import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evidence_fixtures import (  # noqa: E402
    ACCOUNT,
    base_env,
    git,
    make_code_origin,
    make_evidence_origin,
    remote_file,
    remote_head,
)

from scripts import cloud_preflight, evidence_publish  # noqa: E402
from scripts import finalize_run_evidence as fre  # noqa: E402
from scripts.prior_reference_bindings import load_reference_bindings  # noqa: E402
from scripts.same_day_pointer import validate_pointer  # noqa: E402
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, slot_run_dir_relative  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 22)
DAY_TEXT = DAY.isoformat()
NOW = datetime(2026, 9, 22, 17, 0, tzinfo=ET)
ORDER = {"ticker": "MA", "order_id": "ord-1", "submitted_amount_usd": 2.5, "status": "filled"}
BUDGET = {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900}
BROKEN_REMOTE = "does-not-exist.git"
# A second account-shaped value, derived at runtime so no new literal lands in the tree
# (tests/test_config_invariants.py greps tracked files for them).
DECOY_ACCOUNT = ACCOUNT.replace("111", "222")


@pytest.fixture(autouse=True)
def synthetic_account(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", ACCOUNT)


@pytest.fixture
def world(tmp_path):
    code_origin, checkout, sha = make_code_origin(tmp_path)
    return SimpleNamespace(
        tmp=tmp_path,
        checkout=checkout,
        sha=sha,
        origin=make_evidence_origin(tmp_path),
        root=tmp_path / "workspace" / "cron_tracking",
    )


def pull(world, *, day=DAY_TEXT):
    """What a fresh container does at Gate 2: verify the pin and pull the store."""
    code = cloud_preflight.main(
        ["--repo-root", str(world.checkout), "--evidence-remote", str(world.origin),
         "--evidence-root", str(world.root), "--trading-day", day],
        env=base_env(DCA_PINNED_SHA=world.sha),
    )
    assert code == 0


def clone(world, name):
    path = world.tmp / name
    git(world.tmp, "clone", str(world.origin), str(path))
    git(path, "checkout", "-B", "main")
    return path


def finalize(root, slot="1330", *, orders=(ORDER,)):
    started = datetime(2026, 9, 22, int(slot[:2]), int(slot[2:]) + 2, tzinfo=ET)
    return fre.finalize(
        slot, started, outcome="ORDERS_PLACED", order_submit_status="submitted",
        root=Path(root), orders=list(orders), budget=BUDGET, now=NOW,
    )


def publish(world, root=None, slot="1330", *, remote=None, day=DAY_TEXT, env=None):
    return evidence_publish.main(
        ["publish", "--evidence-root", str(root or world.root),
         "--remote", str(remote if remote is not None else world.origin),
         "--trading-day", day, "--slot", slot, "--code-sha", world.sha,
         "--max-attempts", "2", "--backoff-seconds", "0"],
        env=env if env is not None else base_env(),
    )


def claim(world, root, claimant, slot="1330", *, remote=None):
    return evidence_publish.main(
        ["claim", "--evidence-root", str(root),
         "--remote", str(remote if remote is not None else world.origin),
         "--trading-day", DAY_TEXT, "--slot", slot, "--claimant", claimant],
        env=base_env(),
    )


def payload(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_publish_pushes_the_slot_evidence_and_updates_the_manifest(world, capsys):
    pull(world)
    result = finalize(world.root)
    assert result["verified"] is True, result["errors"]

    assert publish(world) == 0
    assert payload(capsys)["status"] == "published"

    run_state_path = f"{slot_run_dir_relative('1330', DAY).as_posix()}/run_state.json"
    published = remote_file(world.origin, run_state_path)
    assert json.loads(published)["orders_submitted_count"] == 1
    # finalize keeps only the fact that an account was pinned; the digits never ship.
    assert ACCOUNT not in published and "account_number_redacted" in published
    manifest = json.loads(remote_file(world.origin, "MANIFEST.json"))
    assert manifest["version"] == 1
    assert manifest["days"][DAY_TEXT]["slots"] == ["1330"]
    assert manifest["last_publish"] == {"day": DAY_TEXT, "slot": "1330", "code_sha": world.sha}


def test_publishing_twice_does_not_duplicate_the_manifest_entry(world, capsys):
    pull(world)
    finalize(world.root)

    assert publish(world) == 0
    capsys.readouterr()
    assert publish(world) == 0

    manifest = json.loads(remote_file(world.origin, "MANIFEST.json"))
    assert manifest["days"][DAY_TEXT]["slots"] == ["1330"]


def test_a_failed_push_keeps_the_evidence_and_alerts_with_the_run_numbers(world, capsys):
    pull(world)
    finalize(world.root)
    run_dir = world.root / slot_run_dir_relative("1330", DAY)
    before = remote_head(world.origin)

    assert publish(world, remote=world.tmp / BROKEN_REMOTE) == 3

    result = payload(capsys)
    assert result["status"] == "push_failed"
    assert remote_head(world.origin) == before
    # Local write comes first and is never undone by a failed push.
    assert (run_dir / "run_state.json").is_file() and (run_dir / "submitted_orders.csv").is_file()
    assert git(world.root, "status", "--porcelain") == ""
    assert "1330" in git(world.root, "log", "-1", "--format=%s")
    assert (world.root / "evidence_push_failed" / f"{DAY_TEXT}_1330.json").is_file()
    # The alert has to stand on its own: the store copy is the one that failed.
    text = result["alert_text"]
    for needle in ("1330", DAY_TEXT, world.sha, "MA", "2.5", "ord-1"):
        assert needle in text, needle
    assert ACCOUNT not in text


def test_a_competing_publish_from_another_container_is_merged_not_lost(world, capsys):
    pull(world)
    finalize(world.root, "1330")
    other = clone(world, "other_container")
    finalize(other, "1430")
    assert publish(world, other, "1430") == 0
    capsys.readouterr()

    assert publish(world, world.root, "1330") == 0

    manifest = json.loads(remote_file(world.origin, "MANIFEST.json"))
    assert sorted(manifest["days"][DAY_TEXT]["slots"]) == ["1330", "1430"]
    for slot in ("1330", "1430"):
        remote_file(world.origin, f"{slot_run_dir_relative(slot, DAY).as_posix()}/run_state.json")


def test_refuses_to_push_the_exact_account_number(world, capsys):
    pull(world)
    finalize(world.root)
    leak = world.root / "intraday_dca" / "20260922_1330ET" / "note.txt"
    leak.write_text(f"placed against account {ACCOUNT}\n", encoding="utf-8")
    before = remote_head(world.origin)

    assert publish(world) == 4

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip().splitlines()[-1])
    assert result["status"] == "refused_secret"
    assert result["refused_paths"] == ["intraday_dca/20260922_1330ET/note.txt"]
    assert remote_head(world.origin) == before
    assert ACCOUNT not in captured.out and ACCOUNT not in captured.err


def test_refuses_an_account_shaped_literal_even_when_the_value_is_unknown(world, capsys):
    pull(world)
    finalize(world.root)
    leak = world.root / "morning_dca" / "20260922_1030ET.json"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text(json.dumps({"account_number": DECOY_ACCOUNT}) + "\n", encoding="utf-8")
    before = remote_head(world.origin)

    assert publish(world, env=base_env(ROBINHOOD_AGENTIC_ACCOUNT_NUMBER="")) == 4

    assert payload(capsys)["refused_paths"] == ["morning_dca/20260922_1030ET.json"]
    assert remote_head(world.origin) == before


def test_second_container_reads_the_first_containers_logged_reference_price(world, capsys):
    day = "2026-09-03"
    pull(world, day=day)
    run_dir = world.root / "market_open_dca" / "20260903_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": day,
            "generated_at": "2026-09-03T09:40:00-04:00",
            "run_classification": {"run_origin": "manual", "run_id": "manual-0940"},
            "buy_candidates": [{
                "ticker": "AAPL", "status": "filled", "order_id": "order-1",
                "planned_amount_usd": 5.0, "target_price": 98.5,
            }],
        }),
        encoding="utf-8",
    )
    assert publish(world, slot="0935", day=day) == 0

    shutil.rmtree(world.root)  # the first container is reclaimed
    pull(world, day=day)  # a brand new container starts empty and pulls

    result = load_reference_bindings(
        [world.root / "market_open_dca", world.root / "morning_dca"], date(2026, 9, 3)
    )
    assert result["verdict"] == "PROCEED"
    assert result["bindings"]["AAPL"] == {
        "amount_usd": 5.0, "reference_price": 98.5, "order_id": "order-1", "resolved": True,
    }


def test_a_pointer_written_in_one_container_validates_in_the_next(world, capsys):
    pull(world)
    result = finalize(world.root)
    assert publish(world) == 0

    shutil.rmtree(world.root)
    pull(world)

    stream, _ = SLOT_EVIDENCE_LAYOUT["1330"]
    pointer = world.root / result["pointer"]
    check = validate_pointer(pointer, expected_stream=stream, expected_root=world.root, today=DAY_TEXT)
    assert check.is_valid


def test_the_first_claim_wins_and_a_stale_second_executor_is_refused(world, capsys):
    pull(world)
    stale = clone(world, "stale_container")

    assert claim(world, world.root, "run-A") == 0
    assert payload(capsys)["status"] == "claimed"

    assert claim(world, stale, "run-B") == 5
    refused = payload(capsys)
    assert refused["status"] == "already_claimed" and refused["claimed_by"] == "run-A"

    # A different slot is a different claim.
    assert claim(world, stale, "run-B", slot="1430") == 0


def test_an_up_to_date_container_is_also_refused_a_slot_that_is_already_claimed(world, capsys):
    pull(world)
    assert claim(world, world.root, "run-A") == 0
    capsys.readouterr()
    fresh = clone(world, "fresh_container")

    assert claim(world, fresh, "run-C") == 5
    assert payload(capsys)["claimed_by"] == "run-A"


def test_claim_fails_closed_when_the_store_is_unreachable(world, capsys):
    pull(world)

    assert claim(world, world.root, "run-A", remote=world.tmp / BROKEN_REMOTE) == 6
    assert payload(capsys)["status"] == "store_unreachable"


def test_parallel_claimants_produce_exactly_one_winner(world, capsys):
    # Not only a stale clone: four containers claiming at the same instant. The remote
    # rejecting the non-fast-forward push is the mutex, so exactly one may win.
    import threading

    pull(world)
    clones = [world.root] + [clone(world, f"racer_{index}") for index in range(1, 4)]
    codes = {}
    barrier = threading.Barrier(len(clones))

    def racer(index):
        barrier.wait()
        codes[index] = claim(world, clones[index], f"run-{index}")

    threads = [threading.Thread(target=racer, args=(index,)) for index in range(len(clones))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(codes.values()) == [0, 5, 5, 5]
    winner = json.loads(remote_file(world.origin, f"claims/{DAY_TEXT}_1330.json"))["claimed_by"]
    assert winner in {f"run-{index}" for index in range(len(clones))}


# --- review follow-ups (Copilot review, PR #1571) -------------------------------------------------------------


def test_a_local_manifest_with_duplicate_keys_is_not_republished(world, capsys):
    pull(world)
    finalize(world.root)
    (world.root / "MANIFEST.json").write_text('{"version": 1, "days": {}, "days": {}}', encoding="utf-8")
    before = remote_head(world.origin)

    assert publish(world) == 3

    assert payload(capsys)["status"] == "push_failed"
    assert remote_head(world.origin) == before


@pytest.mark.parametrize(
    "text",
    [
        '{"version": 1, "days": {}, "days": {}}',
        '{"version": 1, "days": {"2026-09-22": {"slots": [], "slots": ["1330"]}}}',
        "[]",
        '{"version": 1}',
    ],
)
def test_a_fetched_manifest_that_is_ambiguous_or_malformed_is_refused_not_emptied(text):
    with pytest.raises(evidence_publish.Failure):
        evidence_publish._manifest_from_text(text)


@pytest.mark.parametrize("claimant", ["run A", "../run", "run;A", ""])
def test_a_claimant_outside_the_run_id_format_is_rejected(world, claimant):
    pull(world)
    before = remote_head(world.origin)

    with pytest.raises(SystemExit) as exit_info:
        claim(world, world.root, claimant)

    assert exit_info.value.code == 2
    assert remote_head(world.origin) == before


def test_a_claimant_holding_the_account_number_is_refused_before_anything_is_committed(world, capsys):
    pull(world)
    before = remote_head(world.origin)
    local_before = git(world.root, "rev-parse", "HEAD")

    assert claim(world, world.root, f"run-{ACCOUNT}") == 4

    captured = capsys.readouterr()
    assert json.loads(captured.out.strip().splitlines()[-1])["status"] == "refused_secret"
    assert ACCOUNT not in captured.out and ACCOUNT not in captured.err
    assert remote_head(world.origin) == before
    assert git(world.root, "rev-parse", "HEAD") == local_before
    assert not (world.root / "claims").exists()


@pytest.mark.parametrize("command", ["publish", "claim"])
def test_a_slot_that_is_not_on_the_roster_is_rejected(world, command):
    pull(world)
    argv = [command, "--evidence-root", str(world.root), "--remote", str(world.origin),
            "--trading-day", DAY_TEXT, "--slot", "1340"]
    argv += ["--code-sha", world.sha] if command == "publish" else ["--claimant", "run-A"]

    with pytest.raises(SystemExit) as exit_info:
        evidence_publish.main(argv, env=base_env())

    assert exit_info.value.code == 2


def test_publish_refuses_a_code_sha_that_is_not_the_pinned_release(world, capsys):
    pull(world)
    finalize(world.root)
    before = remote_head(world.origin)

    assert publish(world, env=base_env(DCA_PINNED_SHA="b" * 40)) == 7

    assert payload(capsys)["status"] == "refused_provenance"
    assert remote_head(world.origin) == before


def test_publish_refuses_evidence_whose_code_version_is_a_different_release(world, capsys):
    pull(world)
    started = datetime(2026, 9, 22, 13, 32, tzinfo=ET)
    fre.finalize("1330", started, outcome="NO_ORDERS", order_submit_status="no_orders_submitted",
                 root=world.root, code_version={"git_sha": "b" * 40}, now=NOW)
    before = remote_head(world.origin)

    assert publish(world, env=base_env(DCA_PINNED_SHA=world.sha)) == 7

    assert payload(capsys)["status"] == "refused_provenance"
    assert remote_head(world.origin) == before


def test_publish_accepts_evidence_whose_code_version_is_the_pinned_release(world, capsys):
    pull(world)
    started = datetime(2026, 9, 22, 13, 32, tzinfo=ET)
    fre.finalize("1330", started, outcome="NO_ORDERS", order_submit_status="no_orders_submitted",
                 root=world.root, code_version={"git_sha": world.sha}, now=NOW)

    assert publish(world, env=base_env(DCA_PINNED_SHA=world.sha)) == 0

    assert payload(capsys)["status"] == "published"
