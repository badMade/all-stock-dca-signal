"""Tests for dca_paths: the shared, deployment-relative artifact root.

Two properties matter here. First, the runtime default (no env var set) must
resolve to a real repo-relative path that exists on any checkout, so the
same-day top-up guard can actually read prior allocations. Second, the override
must actually redirect every stream, which is the point of the change.

The bundled production path (``BUNDLED_CRON_TRACKING_ROOT``) is kept only as a
rebasing sentinel -- ``config/*.json`` stores ~40 artifact paths against that
string and ``rebase_bundled_root()`` keys off it to rewrite them onto the
configured tree at load time.
"""

from __future__ import annotations

import importlib
import ntpath
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dca_paths import (
    BUNDLED_CRON_TRACKING_ROOT,
    CRON_TRACKING_ROOT_ENV,
    DEFAULT_CRON_TRACKING_ROOT,
    cron_tracking_root,
    rebase_bundled_root,
    rebase_bundled_roots_in,
    stream_root,
)

REPO_ROOT = Path(__file__).resolve().parent

# The per-stream roots, as (module, attribute, expected suffix under the root).
# Kept explicit rather than discovered so a stream that silently stops deriving
# from the shared root shows up as a failure here.
STREAM_ROOTS = [
    ("morning_dca", "DEFAULT_MORNING_LIVE_STATE_ROOT", "morning_dca"),
    ("morning_dca", "DEFAULT_MORNING_WATCHLIST_STATE_ROOT", "morning_dca_watchlist"),
    ("morning_dca", "DEFAULT_MORNING_PREFLIGHT_STATE_ROOT", "morning_dca_preflight"),
    ("morning_dca", "DEFAULT_AFTERNOON_TASK_ROOT", "ce16ba6b"),
    ("morning_dca", "DEFAULT_MARKET_OPEN_TASK_ROOT", "market_open_dca"),
    ("morning_dca", "DEFAULT_INTRADAY_TASK_ROOT", "intraday_dca"),
    ("midday_dca", "DEFAULT_MIDDAY_LIVE_STATE_ROOT", "midday_dca"),
    ("dca_alerts", "DEFAULT_ATTEMPT_LOG_ROOT", "attempt_log"),
    ("scripts.run_classification", "STATE_ROOT", "ce16ba6b"),
    ("scripts.model_usage_log", "DEFAULT_LOG_ROOT", "model_usage"),
]


def test_default_root_is_the_repo_relative_path(monkeypatch):
    """With no override, cron_tracking_root() returns the repo-local default."""
    monkeypatch.delenv(CRON_TRACKING_ROOT_ENV, raising=False)
    assert cron_tracking_root() == DEFAULT_CRON_TRACKING_ROOT


def test_env_override_replaces_the_root(monkeypatch):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "/srv/dca")
    assert cron_tracking_root() == "/srv/dca"
    assert stream_root("morning_dca") == "/srv/dca/morning_dca"


def test_an_empty_override_is_treated_as_unset(monkeypatch):
    """A blank export must not redirect artifacts to the filesystem root."""
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "")
    assert cron_tracking_root() == DEFAULT_CRON_TRACKING_ROOT


def test_stream_root_joins_multiple_segments(monkeypatch):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "/srv/dca")
    assert stream_root("morning_dca", "20260728_1030ET") == "/srv/dca/morning_dca/20260728_1030ET"


@pytest.mark.parametrize("module_name,attr,suffix", STREAM_ROOTS)
def test_every_stream_root_defaults_to_the_repo_relative_path(module_name, attr, suffix):
    """Pin that every stream constant derives from the shared repo-relative root.

    These were 15 separate hardcoded absolute strings before the consolidation;
    this asserts they all derive from DEFAULT_CRON_TRACKING_ROOT, which is now
    the repo-local cron_tracking/ directory rather than the legacy Linux path.
    """
    module = importlib.import_module(module_name)
    assert getattr(module, attr) == f"{DEFAULT_CRON_TRACKING_ROOT}/{suffix}"


def test_every_stream_root_follows_the_override():
    """The override has to reach constants resolved at import time, so this
    re-imports the modules in a fresh interpreter with the variable set --
    exactly how the cron deployment invokes them."""
    probe = "; ".join(
        [
            "import importlib",
            "mods = " + repr([(m, a, s) for m, a, s in STREAM_ROOTS]),
            "print('\\n'.join(f'{a}={getattr(importlib.import_module(m), a)}|{s}' "
            "for m, a, s in mods))",
        ]
    )
    env = dict(os.environ, **{CRON_TRACKING_ROOT_ENV: "/srv/dca-test"})
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in out.stdout.splitlines() if line]
    assert len(lines) == len(STREAM_ROOTS)
    for line in lines:
        assigned, suffix = line.rsplit("|", 1)
        _, value = assigned.split("=", 1)
        assert value == f"/srv/dca-test/{suffix}", line


# --- Bundled-config rebasing -------------------------------------------------
# config/*.json records artifact locations as absolute paths under the bundled
# tree (BUNDLED_CRON_TRACKING_ROOT) and the loaders write them straight into
# the executors, so without this they OVERRIDE the stream_root()-derived
# defaults: $DCA_CRON_TRACKING_ROOT moved every dca_paths consumer while the
# executors kept using the bundled production tree (Codex review, PR #219).

def test_rebase_rewrites_a_bundled_path(monkeypatch):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "/srv/dca")
    assert (
        rebase_bundled_root(f"{BUNDLED_CRON_TRACKING_ROOT}/morning_dca/20260728_1030ET/")
        == "/srv/dca/morning_dca/20260728_1030ET/"
    )


def test_rebase_is_a_no_op_when_configured_root_matches_bundled(monkeypatch):
    """Short-circuit: if the operator configured the exact bundled Linux path,
    rewriting is unnecessary and the value is returned unchanged."""
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, BUNDLED_CRON_TRACKING_ROOT)
    original = f"{BUNDLED_CRON_TRACKING_ROOT}/midday_dca"
    assert rebase_bundled_root(original) == original


def test_rebase_leaves_unrelated_paths_and_non_strings_alone(monkeypatch):
    """An operator who wrote an explicit absolute path meant that exact path."""
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "/srv/dca")
    assert rebase_bundled_root("/mnt/elsewhere/morning_dca") == "/mnt/elsewhere/morning_dca"
    assert rebase_bundled_root("run_state.json") == "run_state.json"
    assert rebase_bundled_root(12) == 12
    assert rebase_bundled_root(None) is None


def test_rebase_walks_nested_config_structures(monkeypatch):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "/srv/dca")
    doc = {
        "artifact_paths": {
            "live_run_state_root": f"{BUNDLED_CRON_TRACKING_ROOT}/midday_dca",
            "prior_allocation_roots": [
                f"{BUNDLED_CRON_TRACKING_ROOT}/morning_dca",
                f"{BUNDLED_CRON_TRACKING_ROOT}/ce16ba6b",
            ],
            "run_state_filename": "run_state.json",
        },
        "trend_min_bars": 6,
    }
    out = rebase_bundled_roots_in(doc)
    assert out["artifact_paths"]["live_run_state_root"] == "/srv/dca/midday_dca"
    assert out["artifact_paths"]["prior_allocation_roots"] == [
        "/srv/dca/morning_dca",
        "/srv/dca/ce16ba6b",
    ]
    assert out["artifact_paths"]["run_state_filename"] == "run_state.json"
    assert out["trend_min_bars"] == 6
    # Input is not mutated.
    assert doc["artifact_paths"]["live_run_state_root"].startswith(BUNDLED_CRON_TRACKING_ROOT)


def test_the_override_reaches_the_executors_not_just_the_defaults():
    """End-to-end: load the real bundled config with the override set.

    This is the regression the unit tests above cannot catch on their own --
    every root the executor actually reads and writes has to move, including the
    ones that arrive from config/*.json rather than from a module constant.
    """
    probe = (
        "from morning_dca import load_morning_config;"
        "from midday_dca import load_midday_config;"
        "m = load_morning_config(mode='live'); d = load_midday_config();"
        "print(m.morning_run_state_root);"
        "print(m.preflight_run_state_root);"
        "print('|'.join(m.preflight_forbidden_roots));"
        "print(d.morning_run_state_root);"
        "print('|'.join(d.prior_allocation_roots))"
    )
    env = dict(
        os.environ,
        **{
            CRON_TRACKING_ROOT_ENV: "/srv/dca-test",
            "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER": "123456789",
        },
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    values = [v for line in out.stdout.splitlines() for v in line.split("|") if v]
    assert values, out.stdout
    for value in values:
        assert value.startswith("/srv/dca-test/"), f"{value} did not follow the override"
        assert DEFAULT_CRON_TRACKING_ROOT not in value


def test_rebase_matches_the_serialized_forward_slash_boundary(monkeypatch):
    """Bundled paths are serialized with "/" on every platform.

    Keying the boundary on os.sep alone meant that on Windows (os.sep == "\\")
    no descendant path matched, so setting the override moved the module-derived
    constants but left the executors on the bundled tree -- readers and writers
    split across two roots (Codex review, PR #219).
    """
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "/srv/dca")
    monkeypatch.setattr("dca_paths.os.sep", "\\")
    assert (
        rebase_bundled_root(f"{BUNDLED_CRON_TRACKING_ROOT}/morning_dca")
        == "/srv/dca/morning_dca"
    )
    # The boundary rule still holds under the simulated separator.
    sibling = f"{BUNDLED_CRON_TRACKING_ROOT}_backup/morning_dca"
    assert rebase_bundled_root(sibling) == sibling


# --- Override normalization --------------------------------------------------
# stream_root() joins through os.path.join (which collapses separators) while
# rebase_bundled_root() concatenates, so a trailing separator on the override
# made the two disagree as strings. MorningDCAExecutor decides whether
# prior_allocation_roots is still at its class default by exact tuple equality,
# so that mismatch made a default config look explicitly overridden -- skipping
# the same-day manual top-up merge and letting a midday run re-buy a ticker an
# earlier manual allocation already covered (Codex review, PR #219).

@pytest.mark.parametrize("configured", ["/srv/dca", "/srv/dca/", "/srv/dca///"])
def test_a_trailing_separator_does_not_change_the_root(monkeypatch, configured):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, configured)
    assert cron_tracking_root() == "/srv/dca"


@pytest.mark.parametrize("configured", ["/srv/dca", "/srv/dca/", "/srv/dca///"])
def test_stream_root_and_rebase_agree_under_a_trailing_separator(monkeypatch, configured):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, configured)
    assert stream_root("morning_dca") == rebase_bundled_root(
        f"{BUNDLED_CRON_TRACKING_ROOT}/morning_dca"
    )


@pytest.mark.parametrize("configured", ["/", "//"])
def test_the_filesystem_root_is_not_stripped_to_nothing(monkeypatch, configured):
    """Pathological but must not produce "" or a doubled separator."""
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, configured)
    assert cron_tracking_root() == "/"
    assert rebase_bundled_root(f"{BUNDLED_CRON_TRACKING_ROOT}/morning_dca") == "/morning_dca"
    assert stream_root("morning_dca") == "/morning_dca"


def test_a_trailing_separator_keeps_prior_allocation_roots_at_their_default():
    """The end-to-end consequence: this exact-tuple comparison gates the
    same-day manual top-up context merge.

    Compares against default_prior_allocation_roots() (resolved via
    stream_root()), not the MiddayConfig class attribute: prior_allocation_roots
    is now a default_factory field (Codex review, PR #219: "Refresh midday
    fallback allocation roots"), and default_factory fields have no class-level
    attribute at all -- MiddayConfig.prior_allocation_roots falls through to
    MorningConfig's own unrelated None default instead. default_prior_allocation_roots()
    is the actual value _compute_top_up's own exact-tuple comparison uses.
    """
    probe = (
        "from midday_dca import load_midday_config;"
        "from morning_dca import default_prior_allocation_roots;"
        "cfg = load_midday_config();"
        "print(tuple(cfg.prior_allocation_roots) == default_prior_allocation_roots())"
    )
    env = dict(
        os.environ,
        **{
            CRON_TRACKING_ROOT_ENV: "/srv/dca-test/",
            "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER": "123456789",
        },
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip().splitlines()[-1] == "True", out.stdout


# --- Root prefixes survive separator trimming --------------------------------
# Trimming trailing separators must not trim away a root PREFIX: bare "C:" is
# drive-relative, so os.path.join("C:", "morning_dca") resolves against the
# process's current directory on that drive rather than the drive root (Codex
# review, PR #219). Exercised with ntpath on any platform so the Windows
# semantics are pinned by a Linux CI run.

@pytest.mark.parametrize(
    "configured,expected",
    [
        ("C:\\", "C:\\"),
        ("C:\\\\", "C:\\"),
        ("C:\\srv\\dca\\", "C:\\srv\\dca"),
        ("\\\\host\\share\\", "\\\\host\\share\\"),
    ],
)
def test_windows_roots_keep_their_drive_prefix(monkeypatch, configured, expected):
    monkeypatch.setattr(os, "sep", "\\")
    monkeypatch.setattr(os.path, "splitdrive", ntpath.splitdrive)
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, configured)
    root = cron_tracking_root()
    assert root == expected
    # The property that actually matters: what gets joined is still absolute.
    assert ntpath.isabs(ntpath.join(root, "morning_dca"))


@pytest.mark.parametrize("configured", ["C:\\dca", "C:\\dca\\"])
def test_rebase_uses_platform_separators(monkeypatch, configured):
    """The bundled values are serialized with forward slashes, so concatenating
    produced "C:\\dca/morning_dca" while stream_root() produced
    "C:\\dca\\morning_dca". Windows resolves both, but the executor compares
    prior_allocation_roots by EXACT string equality (Codex review, PR #219)."""
    monkeypatch.setattr(os, "sep", "\\")
    monkeypatch.setattr(os.path, "join", ntpath.join)
    monkeypatch.setattr(os.path, "splitdrive", ntpath.splitdrive)
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, configured)

    assert stream_root("morning_dca") == rebase_bundled_root(
        f"{BUNDLED_CRON_TRACKING_ROOT}/morning_dca"
    )


def test_default_roots_and_loaded_roots_agree_after_a_late_override():
    """The DEFAULT_*_ROOT constants bind at import; config roots are rebased when
    the config is READ. Setting the override between those moments left the two
    sides disagreeing, so the executor's exact-tuple comparison concluded the
    roots had been explicitly overridden -- skipping the same-day manual top-up
    merge and letting a midday run re-buy a ticker (Codex review, PR #219)."""
    probe = (
        "import os\n"
        "import morning_dca, midday_dca\n"  # imported BEFORE the override
        "os.environ['DCA_CRON_TRACKING_ROOT'] = os.environ['PROBE_ROOT']\n"
        "cfg = midday_dca.load_midday_config()\n"
        "print(tuple(cfg.prior_allocation_roots) == "
        "morning_dca.default_prior_allocation_roots())"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = "/srv/dca-late-override"
    env["ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"] = "999000111"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip().splitlines()[-1] == "True", out.stdout


def test_the_loaders_resolve_the_model_usage_root_at_load_time():
    """The loaders seeded model_usage_log_root from the IMPORT-time constant, so
    an override landing between import and config load rebased the run-state
    roots while leaving model-usage evidence -- and the attempt-log sibling
    derived from it -- under the old tree, where the call-time credit-usage
    report never looks (Codex review, PR #219)."""
    probe = (
        "import os\n"
        "import morning_dca, midday_dca\n"  # imported BEFORE the override
        "os.environ['DCA_CRON_TRACKING_ROOT'] = os.environ['PROBE_ROOT']\n"
        "m = morning_dca.load_morning_config("
        "mode=morning_dca.MORNING_LIVE_MODE, allow_unpinned_account=True)\n"
        "d = midday_dca.load_midday_config(allow_unpinned_account=True)\n"
        "print(m.model_usage_log_root)\nprint(d.model_usage_log_root)"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = "/srv/dca-model-usage"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    roots = [line for line in out.stdout.splitlines() if line]
    assert roots == ["/srv/dca-model-usage/model_usage"] * 2, out.stdout


# --- The unset default must resolve on THIS machine --------------------------
#
# BUNDLED_CRON_TRACKING_ROOT ("/home/user/workspace/cron_tracking") is the
# Perplexity-era Linux path baked into config/*.json. It does not exist on the
# Mac that now runs the seven-slot roster, and an absent prior-allocation root
# "contributes nothing, so this is safe on days a stream did not run" -- except
# here it is absent EVERY day, so the same-day top-up guard degrades to "no
# prior allocation found" and every slot buys full size. It fails open, silently.
# See the now-removed GATE5 proposal doc's section 1.5 (PRODUCTION_READINESS.md gap #1).
#
# The sentinel value itself must NOT change: config/*.json records ~40 artifact
# paths against that exact prefix and rebase_bundled_root() keys off it.
# DEFAULT_CRON_TRACKING_ROOT is now the repo-relative cron_tracking/ directory
# so an unconfigured checkout resolves to a real, writable local path.


def test_bundled_sentinel_value_is_unchanged():
    """rebase_bundled_root() and every config/*.json path key off this string."""
    assert BUNDLED_CRON_TRACKING_ROOT == "/home/user/workspace/cron_tracking"


def test_unset_default_resolves_inside_the_repo(monkeypatch):
    """With no override, the root must be a real, writable, repo-relative path."""
    monkeypatch.delenv(CRON_TRACKING_ROOT_ENV, raising=False)
    resolved = Path(cron_tracking_root())
    assert resolved.is_absolute()
    assert resolved == REPO_ROOT / "cron_tracking"
    # The whole point: a path that actually exists here, so the top-up guard
    # reads real prior allocations instead of finding nothing.
    assert resolved.parent.is_dir()


def test_unset_default_is_no_longer_the_unreachable_linux_path(monkeypatch):
    monkeypatch.delenv(CRON_TRACKING_ROOT_ENV, raising=False)
    assert cron_tracking_root() != BUNDLED_CRON_TRACKING_ROOT
    assert cron_tracking_root() != "/home/user/workspace/cron_tracking"


def test_the_repo_state_root_is_gitignored():
    """Run state is local operational data and must never be committed."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "cron_tracking/" in [line.strip() for line in ignored]


def test_env_override_still_wins_over_the_repo_default(monkeypatch):
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "/srv/dca")
    assert cron_tracking_root() == "/srv/dca"


def test_bundled_config_paths_rebase_onto_the_repo_default(monkeypatch):
    """The reason the sentinel and the default had to become two things.

    rebase_bundled_root() short-circuits when the configured root equals the
    bundled one. With them now distinct, an unconfigured checkout must still
    rewrite bundled config paths onto the repo tree -- otherwise the executors
    read the bundled tree while dca_paths' own constants point at the repo, and
    readers and writers split across two roots (the PR #219 failure mode).
    """
    monkeypatch.delenv(CRON_TRACKING_ROOT_ENV, raising=False)
    rebased = rebase_bundled_root(f"{BUNDLED_CRON_TRACKING_ROOT}/midday_dca")
    assert rebased == str(REPO_ROOT / "cron_tracking" / "midday_dca")


def test_an_explicit_unrelated_absolute_path_is_still_untouched(monkeypatch):
    monkeypatch.delenv(CRON_TRACKING_ROOT_ENV, raising=False)
    assert rebase_bundled_root("/srv/elsewhere/midday_dca") == "/srv/elsewhere/midday_dca"
