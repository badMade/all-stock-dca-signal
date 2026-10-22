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


def test_architecture_page_ships_resizable_sidebars_without_inline_script():
    # Both sidebars were fixed at 260px / 360px, truncating long signatures in the detail pane. The shell now
    # carries two separator handles, the grid reads its column widths from variables, and the drag code lives in
    # the same-origin script so DEFAULT_CSP stays strict.
    from dca_console_diagnostics import PAGE_CSS, PAGE_JS, render_page

    page = render_page()
    assert b'id="grip-l"' in page and b'id="grip-r"' in page and b"<script>" not in page
    assert page.count(b'role="separator"') == 2
    assert "var(--lw,260px)" in PAGE_CSS and "var(--rw,360px)" in PAGE_CSS
    assert "setPointerCapture" in PAGE_JS and "ArrowLeft" in PAGE_JS and "dblclick" in PAGE_JS


def test_every_scanned_module_has_a_plain_english_note_and_no_stale_ones():
    # The detail pane opens with a hand-written plain-English note per module. A new module with no note, or a
    # note for a module that was renamed or deleted, fails here so the map never shows a blank or a ghost.
    from pathlib import Path

    from dca_console_diagnostics import PLAIN_NOTES_FILE, _plain_notes

    repo = Path(__file__).resolve().parents[1]
    notes = _plain_notes(repo)
    scanned = {m["id"]: m for m in build_architecture(repo)["modules"]}
    assert (repo / PLAIN_NOTES_FILE).is_file()
    assert sorted(set(scanned) - set(notes)) == [], "add a plain-English note to docs/architecture_plain_english.json"
    assert sorted(set(notes) - set(scanned)) == [], "remove notes for modules that are no longer scanned"
    assert all(len(note.split()) >= 6 for note in notes.values())
    assert scanned["dca_fileio"]["plain"] == notes["dca_fileio"]


def test_plain_notes_tolerate_a_missing_or_broken_file_and_functions_carry_a_summary(tmp_path):
    from dca_console_diagnostics import _plain_notes, build_detail

    assert _plain_notes(tmp_path) == {}
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture_plain_english.json").write_text("{not json")
    assert _plain_notes(tmp_path) == {}
    (tmp_path / "docs" / "architecture_plain_english.json").write_text('["not", "a", "dict"]')
    assert _plain_notes(tmp_path) == {}
    (tmp_path / "mod.py").write_text(
        'def fetch(x: int) -> int:\n    """Fetch one thing. It then does more detail that the pane must not show."""\n    return x\n'
        'class K:\n    def run(self) -> None:\n        """Run it."""\n')
    detail = build_detail(tmp_path, "mod")
    assert detail["functions"][0]["doc"] == "Fetch one thing."
    assert detail["classes"][0]["methods"][0]["doc"] == "Run it."


def test_other_layer_is_split_into_subcategories_and_nothing_is_left_unsorted():
    # "Other" held 71 of 146 modules as one grey pile. It stays a layer but every Other module now carries a
    # named subcategory; a new Other module that no group claims shows up as "unsorted" and fails here.
    from pathlib import Path

    from dca_console_diagnostics import OTHER_GROUPS, UNSORTED_GROUP, _group_for

    data = build_architecture(Path(__file__).resolve().parents[1])
    other = [m for m in data["modules"] if m["layer"] == "other"]
    assert other and all(m["group"] for m in other)
    assert all(m["group"] == "" for m in data["modules"] if m["layer"] != "other")
    assert [m["id"] for m in other if m["group"] == UNSORTED_GROUP[0]] == [], "give new modules a home in OTHER_GROUPS"
    assert [g["id"] for g in data["groups"]] == [gid for gid, _, _ in OTHER_GROUPS]
    members = [name for _, _, names in OTHER_GROUPS for name in names]
    assert len(members) == len(set(members)), "a module is listed in two subcategories"
    assert sorted(set(members) - {m["id"] for m in other}) == [], "OTHER_GROUPS names a module that is not in Other"
    assert _group_for("brand_new_helper", "other") == UNSORTED_GROUP[0] and _group_for("dca_risk", "money_risk") == ""


def test_architecture_page_assets_render_the_subcategories():
    from dca_console_diagnostics import PAGE_CSS, PAGE_JS

    assert "<optgroup" in PAGE_JS and "colorOf" in PAGE_JS and 'class="subs"' in PAGE_JS.replace("className=\"subs\"", 'class="subs"')
    for gid in ("config_data", "reporting_ops", "broker_market", "research", "utilities", "unsorted"):
        assert f".g-{gid}{{" in PAGE_CSS and f"{gid}:" in PAGE_JS
