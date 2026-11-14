"""The collection-error fix hint printed by the root conftest.py (2026-11-14).

A test file written before the module it tests (test-first) fails collection with
ModuleNotFoundError, and a collection error stops the WHOLE core pytest run, not just that file
(#2266 did this to tests/test_dca_goals.py and tests/test_dca_console_goals.py). The hint tells the
author the repo's fix: gate the file with pytest.importorskip.
"""

from __future__ import annotations

from types import SimpleNamespace

import conftest


class _Reporter:
    def __init__(self, errors):
        self.stats = {"error": errors} if errors is not None else {}
        self.lines: list[str] = []

    def write_line(self, line: str, **_markup) -> None:
        self.lines.append(line)


def _collect_error(text: str):
    # Only the attributes the hook reads: a failed CollectReport's `when` is "collect".
    return SimpleNamespace(when="collect", failed=True, longreprtext=text, nodeid="tests/test_new.py")


def test_hint_names_importorskip_for_a_missing_module_at_collection():
    reporter = _Reporter([_collect_error("E   ModuleNotFoundError: No module named 'dca_goals'")])
    conftest.pytest_terminal_summary(reporter, 2, None)
    assert len(reporter.lines) == 1
    assert "pytest.importorskip" in reporter.lines[0] and "tests/test_new.py" in reporter.lines[0]


def test_no_hint_without_an_import_error_at_collection():
    for errors in (None, [], [_collect_error("E   SyntaxError: invalid syntax")],
                   [SimpleNamespace(when="call", failed=True, longreprtext="ImportError: x", nodeid="t::a")]):
        reporter = _Reporter(errors)
        conftest.pytest_terminal_summary(reporter, 1, None)
        assert reporter.lines == [], errors
