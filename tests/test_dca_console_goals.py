"""Tests written FIRST for the console Goals page (docs/adr-goals-and-loss-hedge-shadow.md, D5).

They are skipped until ``dca_console_goals.py`` lands (with ``POST /api/goals/propose``):

* Test 1 (HTTP half): the route rejects bad proposals before writing anything.
* Test 2: the route keeps ``/api/jobs``' guard order (untrusted 403, Basic 401, token 403).
* Test 3: no console code writes under ``config/``; the one new write location is
  ``<console-output>/goal-proposals/``.

The console stays a proposer. Merging the PR that the ``propose-goal`` job opens is the only
way a goal reaches ``config/goals.json``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
from test_dca_console import AUTH_CREDENTIAL, auth_headers, request

import dca_console
from dca_console import AuthFailureLog, ConsoleAccess, ConsoleApp, Settings, make_server

REPO = Path(__file__).resolve().parents[1]
ROUTE = "/api/goals/propose"
PROPOSALS = "goal-proposals"

# Far enough ahead that the server's real clock never makes it "past" during the suite's life.
FUTURE = "2030-01-01T00:00:00-05:00"

CURRENT_GOALS = {
    "$schema": "../schemas/goals.schema.json", "version": 1, "status": "active",
    "goals": [{"goal_id": "growth-twr-5y", "goal_version": 1, "kind": "aspirational", "metric": "twr_cagr",
               "horizon_years": 5, "target": 0.20, "benchmark": "SP500_TR",
               "effective_from": "2026-11-01T00:00:00-05:00"}],
}


def proposal_body(**overrides) -> bytes:
    entry = {"goal_id": "growth-twr-5y", "kind": "aspirational", "metric": "twr_cagr", "horizon_years": 5,
             "target": 0.15, "benchmark": "SP500_TR", "effective_from": FUTURE}
    entry.update(overrides)
    return json.dumps({"goal": entry}).encode()


@pytest.fixture
def config_dir(tmp_path):
    path = tmp_path / "config"
    path.mkdir()
    (path / "goals.json").write_text(json.dumps(CURRENT_GOALS, indent=2))
    # A second, unrelated config file proves the console touches nothing else in config/ either.
    (path / "budgets.json").write_text('{"note": "must stay byte-identical"}')
    return path


@pytest.fixture
def app(tmp_path, monkeypatch, config_dir):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings("local", tmp_path / "artifacts", tmp_path / "output", date(2026, 11, 13),
                                      sys.executable, sys.executable, config_dir=config_dir))
    yield application
    application.jobs.close()


@contextmanager
def serving(app, access=None, failure_log=None):
    kwargs = {} if access is None else {"access": access, "failure_log": failure_log}
    server = make_server(app, 0, **kwargs)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.fixture
def http_server(app):
    with serving(app) as server:
        yield server


@pytest.fixture
def auth_server(app):
    import io

    access = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL)
    with serving(app, access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        yield server


def propose(server, token, body: bytes, **headers):
    return request(server, ROUTE, method="POST", body=body,
                   headers={"X-Console-Token": token, "Content-Type": "application/json", **headers})


def proposals_dir(tmp_path: Path) -> Path:
    return tmp_path / "output" / PROPOSALS


def tree_digest(root: Path) -> dict[str, tuple[str, int]]:
    """Every file under ``root`` mapped to (sha256, mtime_ns), so any write is visible."""
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


# --------------------------------------------------------------------------------------------
# Test 1 (HTTP half): bad proposals are rejected with 400 and nothing is written.
# --------------------------------------------------------------------------------------------

def test_1_a_valid_proposal_is_saved_as_the_next_version(app, http_server, tmp_path):
    status, _, body = propose(http_server, app.token, proposal_body())
    assert status == 202, body
    assert json.loads(body) == {"proposal": "growth-twr-5y-v2.json"}
    saved = json.loads((proposals_dir(tmp_path) / "growth-twr-5y-v2.json").read_text())
    assert saved["goal"]["target"] == pytest.approx(0.15)
    assert saved["goal_version"] == 2


@pytest.mark.parametrize("body", [
    b"{",
    b"[]",
    b'{"goal": {}, "goal": {}}',
    proposal_body(surprise=1),
    proposal_body(target=0.9),
    proposal_body(target=-0.1),
    proposal_body(effective_from="2030-01-01T00:00:00"),
    proposal_body(effective_from="2020-01-01T00:00:00-05:00"),
    proposal_body(goal_version=9),
    proposal_body(goal_id="../../config/budgets"),
    b"x" * 1025,
])
def test_1_bad_proposals_return_400_and_write_nothing(app, http_server, tmp_path, body):
    status, _, response = propose(http_server, app.token, body)
    assert status == 400, response
    assert "error" in json.loads(response)
    assert not proposals_dir(tmp_path).exists()


def test_1_a_hedge_proposal_asking_for_active_is_rejected(app, http_server, tmp_path):
    body = json.dumps({"goal": {"goal_id": "loss-hedge", "kind": "loss_hedge", "mode": "active",
                                "max_drawdown": 0.25, "hedge_budget_pct": 0.02, "instruments": [],
                                "effective_from": FUTURE}}).encode()
    assert propose(http_server, app.token, body)[0] == 400
    assert not proposals_dir(tmp_path).exists()


# --------------------------------------------------------------------------------------------
# Test 2: the same guard order as /api/jobs, and a rejected request writes nothing.
# --------------------------------------------------------------------------------------------

def test_2_missing_or_wrong_token_is_403_without_auth_configured(app, http_server, tmp_path):
    for token in ("", "incorrect-token"):
        assert propose(http_server, token, proposal_body())[0] == 403
    assert not proposals_dir(tmp_path).exists()


def test_2_untrusted_origin_is_403_before_anything_else(app, http_server, tmp_path):
    for attack in ({"Origin": "https://untrusted.example"}, {"Host": "untrusted.example"},
                   {"Sec-Fetch-Site": "cross-site"}):
        status, headers, _ = propose(http_server, app.token, proposal_body(), **attack)
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers
    assert not proposals_dir(tmp_path).exists()


def test_2_basic_auth_is_checked_before_the_token(app, auth_server, tmp_path):
    # No Basic credential: 401 even though the token is right.
    assert propose(auth_server, app.token, proposal_body())[0] == 401
    # Right Basic credential, wrong token: 403.
    assert propose(auth_server, "incorrect-token", proposal_body(), **auth_headers())[0] == 403
    assert not proposals_dir(tmp_path).exists()
    # Both right: accepted.
    assert propose(auth_server, app.token, proposal_body(), **auth_headers())[0] == 202


def test_2_the_route_is_post_only(app, http_server):
    assert request(http_server, ROUTE)[0] in {404, 405}


# --------------------------------------------------------------------------------------------
# Test 3: the console never writes under config/. Proposals are its only new write location.
# --------------------------------------------------------------------------------------------

def test_3_a_proposal_leaves_config_untouched_and_writes_only_the_proposal(app, http_server, tmp_path,
                                                                          config_dir):
    config_before = tree_digest(config_dir)
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    output_before = tree_digest(output)

    assert propose(http_server, app.token, proposal_body())[0] == 202

    assert tree_digest(config_dir) == config_before, "the console wrote under config/"
    new_files = set(tree_digest(output)) - set(output_before)
    assert new_files == {f"{PROPOSALS}/growth-twr-5y-v2.json"}


def test_3_the_goals_module_has_no_subprocess_network_or_broker_import():
    # The git/gh step belongs to the allowlisted propose-goal job in dca_console_jobs, not to the
    # page reader. The reader validates and saves a proposal file; nothing more.
    tree = ast.parse((REPO / "dca_console_goals.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"subprocess", "socket", "urllib", "http", "requests", "shutil", "near_close_dca", "run_dca",
                 "dca_runner", "dca_live_adapters"}
    assert not imported & forbidden, sorted(imported & forbidden)


def test_3_propose_goal_is_an_allowlisted_job_action(app):
    # It joins ConsoleApp.actions()' closed list like every other job; the browser still posts
    # only {"action": "propose-goal"} to /api/jobs, never a command or a path.
    assert "propose-goal" in {row["id"] for row in app.actions()}
