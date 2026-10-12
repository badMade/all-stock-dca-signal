"""Stage 2 / PR B contract: nothing in the live program depends on the retired 09:45 pre-flight.

Owner direction (2026-10-08): "I do not have any need for the 9:45 pre-flight. That was an old part of this
program that should have been removed long ago. There should be nothing left that is dependent on the
pre-flight." These tests were written before the removal and fail until it lands.

The word "preflight" has other, unrelated meanings in this repo (``scripts/cloud_preflight.py``, the generic
``PREFLIGHT`` lifecycle state in ``scripts/run_state_machine.py``, the DNS pre-check in ``dca_net_utils.py``).
Those stay, and ``test_unrelated_preflight_meanings_survive`` guards against over-deletion. The scan below
therefore looks for the retired 09:45 vocabulary, not the bare word.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Identifiers and phrases that only ever meant the 09:45 morning pre-flight.
RETIRED = re.compile(
    "|".join(
        [
            r"morning[_ -]?pre-?flight",
            r"MORNING_PREFLIGHT",
            r"morning-dca-0945",
            r"morning_dca_preflight",
            r"preflight_gate",
            r"latest_preflight",
            r"gfuz_supplemental_preflight",
            r"supplemental_preflight",
            r"load_same_day_preflight",
            r"preflight_admitted_tickers",
            r"_apply_preflight_gate",
            r"requires_preflight_artifact",
            r"preflight_run_state_root",
            r"preflight_artifact",
            r"preflight_forbidden_roots",
            r"preflight_bounded_recovery",
            r"also_runs_required_preflight_writer",
            r"(?:9:45|09:45)\s+pre-?flight",
            r"same-day\s+pre-?flight",
        ]
    ),
    re.IGNORECASE,
)

# The 9:45 WATCHLIST scan (cron bb4f6ef1, MORNING_WATCHLIST_*) is a different, read-only analytics run that shared the
# 09:45 slot with the pre-flight writer. This PR retires only the pre-flight; the scan stays.
ALLOWED_RETROSPECTIVE: dict[str, str] = {}

SCANNED_GLOBS = (
    "*.py",
    "scripts/*.py",
    "dca_*.py",
    "config/*.json",
    "schemas/*.json",
    "claude/dca-executor-prompts/**/*.txt",
    "claude/dca-executor-prompts/**/*.md",
    "deploy/**/*.py",
    "deploy/**/*.sh",
    "deploy/**/*.md",
)


def _scanned_files():
    seen = set()
    for pattern in SCANNED_GLOBS:
        for path in ROOT.glob(pattern):
            if not path.is_file() or "tests" in path.relative_to(ROOT).parts:
                continue
            seen.add(path)
    return sorted(seen)


# Dated change notes, retirement records and supersession provenance legitimately name what was retired. They are
# history, not dependencies, so the JSON scan skips them; every operative key is still scanned.
_HISTORY_KEY = re.compile(
    r"^change_note|_20\d\d_\d\d|^retired_not_in_canonical_schedule$|^supersedes_task_ids$|^supersedes_cron_id$|^added_reason$"
    r"|^must_not_reintroduce$|^id_note$"
)


def _json_strings(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if _HISTORY_KEY.search(key):
                continue
            yield key
            yield from _json_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _json_strings(item)
    elif isinstance(node, str):
        yield node


def _scan_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        return "\n".join(_json_strings(json.loads(text)))
    return text


def test_no_live_code_config_schema_or_prompt_mentions_the_retired_preflight():
    offenders = {}
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_RETROSPECTIVE or rel == "tests/test_preflight_retired.py":
            continue
        hits = sorted({m.group(0).lower() for m in RETIRED.finditer(_scan_text(path))})
        if hits:
            offenders[rel] = hits[:6]
    assert offenders == {}, json.dumps(offenders, indent=1)


@pytest.mark.parametrize(
    ("module", "names"),
    [
        (
            "morning_dca",
            [
                "MORNING_PREFLIGHT_MODE",
                "MORNING_PREFLIGHT_CRON_ID",
                "MORNING_PREFLIGHT_RUN_TYPE",
                "DEFAULT_MORNING_PREFLIGHT_STATE_ROOT",
                "preflight_eligibility",
                "preflight_ticker_eligibility_row",
                "default_preflight_forbidden_roots",
                "load_same_day_preflight",
                "morning_preflight_eligible_tickers",
            ],
        ),
        ("scripts.generate_morning_candidates", ["preflight_admitted_tickers"]),
        ("scripts.scheduled_artifacts", ["PREFLIGHT_CRON_ID", "PREFLIGHT_RUN_TYPE", "build_preflight_run_classification"]),
        ("scripts.run_classification", ["morning_preflight_blocked_run_state"]),
    ],
)
def test_the_preflight_api_is_gone(module, names):
    mod = importlib.import_module(module)
    assert [n for n in names if hasattr(mod, n)] == []


def test_the_morning_executor_has_no_preflight_gate_and_scores_the_full_universe():
    import morning_dca
    from scripts.stream_candidates import StreamSpec

    assert not hasattr(morning_dca.MorningDCAExecutor, "_apply_preflight_gate")
    assert "universe_filter" not in {f.name for f in dataclasses.fields(StreamSpec)}


def test_scheduled_tasks_drop_the_preflight_entry_and_the_writer_clause_but_keep_the_watchlist_scan():
    tasks = json.loads((ROOT / "config" / "scheduled_tasks.json").read_text())["tasks"]
    by_id = {str(t.get("id") or t.get("task_id") or ""): t for t in tasks}
    assert "morning-dca-0945" not in by_id
    assert all(t.get("executor_mode") != "preflight" for t in tasks)
    # The 09:45 slot's watchlist scan survives, without the pre-flight writer it used to also run.
    assert "bb4f6ef1" in by_id
    # Kept active at the owner's direction (2026-10-12): only the pre-flight was retired, not the scan.
    assert by_id["bb4f6ef1"]["status"] == "active"
    assert by_id["bb4f6ef1"].get("in_live_scheduler") is not False
    assert "preflight" not in json.dumps(by_id["bb4f6ef1"]).lower()


def test_the_watchlist_scan_survives():
    import morning_dca

    assert morning_dca.MORNING_WATCHLIST_MODE == "watchlist"
    assert morning_dca.MORNING_WATCHLIST_CRON_ID == "bb4f6ef1"


def test_the_morning_config_has_no_preflight_keys():
    config = json.loads((ROOT / "config" / "morning_dca.json").read_text())

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    assert [k for k in keys(config) if "preflight" in k.lower()] == []


def test_the_preflight_schema_is_gone():
    assert not (ROOT / "schemas" / "morning_preflight.schema.json").exists()


def test_unrelated_preflight_meanings_survive():
    # Over-deletion guard: these share the word, not the 09:45 task.
    from scripts import run_state_machine

    assert run_state_machine.PREFLIGHT == "preflight"
    assert (ROOT / "scripts" / "cloud_preflight.py").is_file()
    assert "preflight" in (ROOT / "dca_net_utils.py").read_text().lower()
