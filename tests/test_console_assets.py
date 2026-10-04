"""The browser bundle contract: an exact asset map, no HTML injection sinks, one section per page."""

import re

from dca_console import ASSETS, CONSOLE_FILES, REPO

CONSOLE = REPO / "console"
SCRIPTS = tuple(name for _, name, kind in CONSOLE_FILES if kind == "text/javascript")


def test_every_console_asset_exists_and_the_map_is_exact():
    assert set(ASSETS) == {route for route, _, _ in CONSOLE_FILES}
    for route, name, kind in CONSOLE_FILES:
        assert (CONSOLE / name).is_file(), route
        assert kind in {"text/html", "text/javascript", "text/css"}
    # No orphan: every file under console/ is served, and only those files are.
    served = {name for _, name, _ in CONSOLE_FILES}
    assert {path.name for path in CONSOLE.iterdir() if path.is_file()} == served


def test_index_loads_only_registered_assets_and_no_inline_script():
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="([^"]+)" defer></script>', html)
    styles = re.findall(r'<link rel="stylesheet" href="([^"]+)">', html)
    assert scripts and styles
    assert set(scripts) | set(styles) <= set(ASSETS)
    # DEFAULT_CSP forbids inline handlers and inline scripts; the markup must
    # not rely on either or the page silently stops working.
    assert html.count("<script") == len(scripts)
    assert not re.search(r"\son[a-z]+=", html)


def test_console_scripts_avoid_html_injection_sinks():
    for name in SCRIPTS:
        source = (CONSOLE / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in source, f"{name} uses {sink}; artifact text must be inserted as text nodes"


def test_every_nav_link_has_a_page_section_in_the_same_order():
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    nav = re.findall(r'<a href="#([a-z]+)" data-page="([a-z]+)">', html)
    assert nav and all(target == page for target, page in nav)
    sections = re.findall(r'<section class="page" data-page="([a-z]+)"', html)
    assert [page for _, page in nav] == sections
    assert sections[0] == "today"
    assert len(sections) == len(set(sections)) == 13
    # Every page except Today starts hidden; the router reveals exactly one.
    assert html.count('class="page" data-page="today"') == 1
    assert all(f'data-page="{page}"' in html and (page == "today" or f'id="page-{page}"' in html) for page in sections)
    assert len(re.findall(r'<section class="page" data-page="[a-z]+" id="page-[a-z]+"(?: data-placeholder)? hidden>', html)) == 12
