"""scripts/backup_planner.py: the Python 3.11 floor for ``python3 -m scripts.backup_planner`` (2026-10-16 incident).

The Mac shell fallback ran Python 3.9 while the repo requires 3.11 or newer. ``backup_planner.pyz`` already refuses an
old interpreter in its bootstrap (``scripts/build_backup_planner.py``), but the Mac prompts also run
``python3 -m scripts.backup_planner ...`` and that form had no guard. Written from the required behaviour, not from
any implementation:

1. A supported interpreter passes the guard silently.
2. An older one is refused with the planner's own failure contract: ONE JSON document ``{"status": "ABORT",
   "plan": null, "errors": [{"code": "python_too_old", ...}]}`` and exit code 2, the same document and code the .pyz
   bootstrap emits, so the prompt's "anything but exit 0 places nothing" rule applies unchanged.
3. The refusal happens before any project import, so a 3.9 interpreter never reaches syntax or import errors.
4. The module and the .pyz bootstrap share one floor, so they cannot drift apart.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scripts import backup_planner as bp
from scripts import build_backup_planner as bbp


@pytest.mark.parametrize("version", [(3, 11, 0), (3, 11, 9), (3, 12, 4), (3, 13, 0), (4, 0, 0)])
def test_a_supported_python_passes_the_guard(version, capsys):
    bp.require_supported_python(version)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("version", [(3, 9, 6), (3, 10, 14), (3, 8, 0), (2, 7, 18)])
def test_an_old_python_is_refused_with_the_planner_abort_document_and_exit_2(version, capsys):
    with pytest.raises(SystemExit) as exit_info:
        bp.require_supported_python(version)
    assert exit_info.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "status": "ABORT",
        "plan": None,
        "errors": [
            {"code": "python_too_old", "detail": f"python>=3.11 required, found {version[0]}.{version[1]}"},
        ],
    }


def test_the_guard_runs_before_any_project_import():
    source = pathlib.Path(bp.__file__).read_text(encoding="utf-8")
    guard_call = source.index("require_supported_python(sys.version_info)")
    assert guard_call < source.index("import dca_config")
    assert guard_call < source.index("from scripts import backup_stream_specs")


def test_the_module_and_the_pyz_bootstrap_share_one_python_floor():
    assert tuple(bp.MIN_PYTHON) == tuple(bbp.DEFAULT_MIN_PYTHON)
