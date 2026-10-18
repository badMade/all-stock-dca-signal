"""The architecture map: a static ast read, served sandboxed, never mutating or importing the scanned code."""

import json
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from dca_console_diagnostics import SCAN_CACHE, ScanCache, _is_bag, build_architecture, build_detail
from dca_console_routes import DEFAULT_CSP, GET_ROUTES, RouteRequest

REPO = Path(__file__).resolve().parent.parent


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "dca_rec_models.py").write_text(textwrap.dedent('''
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Recommendation:
            ticker: str
            extra: dict
        def helper(payload: dict, n: int) -> int:
            return n
        def _hidden() -> None: ...
    '''))
    (tmp_path / "dca_recommender.py").write_text(textwrap.dedent('''
        from dca_rec_models import Recommendation, helper, _hidden
        import dca_rec_models as models
        def compute(x):
            helper({}, 1)
            models.helper({}, 2)
            return Recommendation("A", {})
    '''))
    (tmp_path / "lonely.py").write_text("x = 1\n")
    (tmp_path / "test_ignored.py").write_text("import dca_rec_models\n")
    (tmp_path / "broken.py").write_text("def (:\n")
    return tmp_path


def test_scan_finds_classes_edges_flags_and_skips_tests_and_broken_files(tmp_path):
    root = _tree(tmp_path)
    data = build_architecture(root)
    mods = {m["id"]: m for m in data["modules"]}
    assert set(mods) == {"dca_rec_models", "dca_recommender", "lonely"}
    assert mods["dca_rec_models"]["layer"] == "plan_first"
    assert mods["dca_rec_models"]["classes"][0]["kind"] == "frozen dataclass"
    assert "fields" not in mods["dca_rec_models"]["classes"][0]  # the whole-repo scan is a cheap skeleton
    detail = build_detail(root, "dca_rec_models")
    assert {f["name"]: f["bag"] for f in detail["classes"][0]["fields"]} == {"ticker": False, "extra": True}
    assert any(p["bag"] for p in detail["functions"][0]["params"])
    edge = next(e for e in data["edges"] if e["from"] == "dca_recommender")
    assert edge["to"] == "dca_rec_models" and edge["private"] == ["_hidden"]
    assert "calls" not in data  # call edges are on demand, not part of the whole-repo scan
    calls = build_detail(root, "dca_recommender")["calls"]
    assert {c["to"] for c in calls if c["from"] == "compute"} == {"dca_rec_models:helper", "dca_rec_models:Recommendation"}
    assert build_detail(root, "nope") is None
    assert mods["lonely"]["unreferenced"] and not mods["dca_rec_models"]["unreferenced"]


def test_scan_output_is_plain_json_even_for_hostile_source(tmp_path):
    root = _tree(tmp_path)
    (root / "evil.py").write_text('def f(a: dict):\n    """</script>"""\nclass C:\n    x: "</script>"\n')
    data = json.loads(json.dumps(build_architecture(root)))
    assert data["stats"]["modules"] == 4


def test_routes_serve_shell_assets_and_data_under_the_strict_default_csp():
    def get(path):
        url = urlsplit(path)
        return GET_ROUTES[url.path](None, RouteRequest(url, {}))

    page = get("/diagnostics/architecture")
    assert page.code == 200 and page.kind == "text/html" and page.csp == DEFAULT_CSP
    assert b"<script>" not in page.body and b"architecture.js" in page.body  # no inline script: DEFAULT_CSP stays strict
    assert get("/diagnostics/architecture.css").kind == "text/css"
    assert get("/diagnostics/architecture.js").kind == "text/javascript"
    first = json.loads(get("/api/diagnostics/architecture?refresh=1").body)
    assert first["status"] in {"building", "ready"}  # the request never blocks on the scan
    SCAN_CACHE.wait()
    ready = json.loads(get("/api/diagnostics/architecture").body)
    assert ready["status"] == "ready" and ready["stats"]["modules"] > 100
    detail = json.loads(get("/api/diagnostics/architecture/module?module=dca_recommender").body)
    assert any(c["to"].startswith("dca_rec_models") for c in detail["calls"])
    assert any(c["name"] == "RecommendationEngine" and c["fields"] is not None for c in detail["classes"])
    assert get("/api/diagnostics/architecture/module?module=no_such_module").code == 404
    with pytest.raises(ValueError):
        get("/api/diagnostics/architecture/module?module=../etc/passwd")


def test_real_repo_shows_plan_first_objects():
    data = build_architecture(REPO)
    classes = {c["name"] for m in data["modules"] for c in m["classes"]}
    assert {"RecommendationSet", "RecommendationEngine", "RecommendationStore"} <= classes
    assert "dca_console_diagnostics" not in {m["id"] for m in data["modules"] if m["layer"] != "console"}


def test_nested_dict_annotations_are_flagged_but_callable_any_is_not():
    for flagged in ("dict", "list[dict[str, Any]]", "Mapping[str, Any]", "Iterable[Mapping]", "Any", "tuple[dict, int]"):
        assert _is_bag(flagged), flagged
    for clean in ("str", "list[str]", "Callable[..., Any]", "Path | None", "tuple[str, ...]"):
        assert not _is_bag(clean), clean


def test_unresolved_bare_calls_make_no_edges_and_local_calls_are_marked(tmp_path):
    (tmp_path / "other.py").write_text("def far():\n    return 1\n")
    (tmp_path / "mod.py").write_text(textwrap.dedent('''
        from pathlib import Path
        import json
        from other import far
        def near(): return 1
        def run(callback):
            isinstance(1, int); Path("x"); json.dumps({}); callback(); len([])
            near()
            far()
    '''))
    calls = build_detail(tmp_path, "mod")["calls"]
    assert {(c["to"], c["local"]) for c in calls} == {("mod:near", True), ("other:far", False)}


def test_scan_cache_builds_in_the_background_and_reports_errors(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    cache = ScanCache()
    assert cache.get(root)["status"] in {"building", "ready"}
    cache.wait()
    assert cache.get(root)["status"] == "ready"
    boom = ScanCache()
    monkeypatch.setattr("dca_console_diagnostics.build_architecture", lambda repo: 1 / 0)
    boom.get(root)
    boom.wait()
    assert boom.get(root) == {"status": "error", "error": "ZeroDivisionError: division by zero"}
