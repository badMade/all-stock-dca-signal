"""Tests for scripts/generate_module_graph.py: the generated docs/module_graph.md stays honest."""

from __future__ import annotations

from pathlib import Path

from scripts import generate_module_graph as gmg


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "__init__.py").write_text("")
    (tmp_path / "base.py").write_text("X = 1\n")
    (tmp_path / "mid.py").write_text("import base\n")
    (tmp_path / "top.py").write_text("from scripts import helper\nfrom mid import thing\n")
    (tmp_path / "scripts" / "helper.py").write_text("from . import sibling\nimport base\n")
    (tmp_path / "scripts" / "sibling.py").write_text("")
    (tmp_path / "test_ignored.py").write_text("import base\n")
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "prompt.txt").write_text("run python -m scripts.helper then top.py\n")
    return tmp_path


def test_discovery_skips_tests_and_resolves_edges(tmp_path):
    modules, forward, reverse = gmg.build_graph(_tree(tmp_path))
    assert "test_ignored" not in modules
    assert forward["mid"] == {"base"}
    assert forward["top"] == {"scripts.helper", "mid"}  # ``from scripts import helper`` is a submodule edge
    assert forward["scripts.helper"] == {"scripts.sibling", "base"}  # relative import resolved
    assert reverse["base"] == {"mid", "scripts.helper"}


def test_impact_is_transitive_and_nearest_first(tmp_path):
    _, _, reverse = gmg.build_graph(_tree(tmp_path))
    assert gmg.impact("base", reverse) == [(1, "mid"), (1, "scripts.helper"), (2, "top")]


def test_external_refs_count_file_and_dash_m_mentions_only(tmp_path):
    root = _tree(tmp_path)
    modules, _, _ = gmg.build_graph(root)
    refs = gmg.external_reference_counts(modules, root)
    assert refs["scripts.helper"] == 1 and refs["top"] == 1 and refs["base"] == 0


def test_render_is_deterministic_and_check_detects_drift(tmp_path):
    root = _tree(tmp_path)
    assert gmg.render(root) == gmg.render(root)
    out = tmp_path / "out.md"
    out.write_text("stale")
    assert gmg.main(["--check", "--output", str(out)]) == 1
    assert gmg.main(["--output", str(out)]) == 0  # rewrites from the real repo...
    assert gmg.main(["--check", "--output", str(out)]) == 0  # ...and is then current


def test_impact_cli_rejects_unknown_module(capsys):
    assert gmg.main(["--impact", "no_such_module"]) == 2
    assert "unknown module" in capsys.readouterr().err
