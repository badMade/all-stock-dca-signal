"""scripts/local_heartbeat.py: the Python 3.11 floor (2026-10-16 09:49 ET incident).

The Mac shell fallback ran Python 3.9 while the repo requires 3.11 or newer (``requires-python`` in pyproject.toml).
Written from the required behaviour, not from any implementation:

1. A supported interpreter passes the guard silently.
2. An older one is refused with one parseable JSON line that names the reason and both versions, and exit code 1, so
   the backup prompt's rule "a non-zero exit is local_status unknown" stands the backup down with a readable cause.
3. The refusal happens before any project import, so a 3.9 interpreter never reaches syntax or import errors.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scripts import local_heartbeat as hb


@pytest.mark.parametrize("version", [(3, 11, 0), (3, 11, 9), (3, 12, 4), (3, 13, 0), (4, 0, 0)])
def test_a_supported_python_passes_the_guard(version, capsys):
    hb.require_supported_python(version)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("version", [(3, 9, 6), (3, 10, 14), (3, 8, 0), (2, 7, 18)])
def test_an_old_python_is_refused_with_parseable_json_and_exit_1(version, capsys):
    with pytest.raises(SystemExit) as exit_info:
        hb.require_supported_python(version)
    assert exit_info.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "ok": False,
        "reason": "python_too_old",
        "required": "3.11",
        "found": ".".join(str(part) for part in version),
    }


def test_the_guard_runs_before_any_project_import():
    source = pathlib.Path(hb.__file__).read_text(encoding="utf-8")
    guard_call = source.index("require_supported_python(sys.version_info)")
    assert guard_call < source.index("import dca_execution_router")
    assert guard_call < source.index("from scripts.file_lock import")
