"""Tests for the Notion and GitHub-branch recommendation publishers (hermetic, no network)."""

from __future__ import annotations

import email.message
import io
import ipaddress
import json
import subprocess
import urllib.response
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import dca_net_utils
import dca_rec_publish as pub
from dca_rec_models import Recommendation, RecommendationSet
from scripts.validate_native_placement import native_ref_id

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 12, 26, tzinfo=ET)
PAGE = "0123456789abcdef0123456789abcdef"


def _set(slot="1230", tickers=("AAPL",)):
    return RecommendationSet(DAY, datetime(2026, 10, 13, 12, 25, tzinfo=ET), slot, "robinhood_agent_fetch",
                             "ladder-v1", "hash-v1",
                             tuple(Recommendation(t, 3.0, native_ref_id(slot, DAY.isoformat(), t)) for t in tickers))


def _doc(**kw):
    return pub.build_document({"1230": _set(**kw)}, generated_at=NOW)


# --- document ------------------------------------------------------------------------------


def test_document_carries_only_agent_safe_fields():
    text = json.dumps(_doc())
    for word in ("price", "close", "low", "quote", "bar", "account"):
        assert word not in text
    assert json.loads(text)["slots"]["1230"]["items"][0]["ticker"] == "AAPL"


def test_document_rejects_unknown_slot_and_naive_time():
    with pytest.raises(ValueError):
        pub.build_document({"1200": _set()}, generated_at=NOW)
    with pytest.raises(ValueError):
        pub.build_document({"1230": _set()}, generated_at=datetime(2026, 10, 13, 12, 26))


# --- keychain ------------------------------------------------------------------------------


def test_keychain_secret_reads_stdout_and_never_puts_a_secret_in_argv():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="secret_token\n")

    assert pub.keychain_secret("all-stock-dca.notion", "integration", runner=runner) == "secret_token"
    assert "secret_token" not in " ".join(calls[0])


@pytest.mark.parametrize("result", [SimpleNamespace(returncode=44, stdout=""), SimpleNamespace(returncode=0, stdout=" \n")])
def test_keychain_secret_missing_is_none(result):
    assert pub.keychain_secret("svc", "acct", runner=lambda argv, **kw: result) is None


def test_keychain_rejects_unsafe_names():
    with pytest.raises(ValueError):
        pub.keychain_secret("svc; rm -rf /", "acct")


# --- notion --------------------------------------------------------------------------------


class FakeNotion:
    def __init__(self, children=(), list_status=200, write_status=200, raw=None):
        self.children = list(children)
        self.list_status = list_status
        self.write_status = write_status
        self.raw = raw
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        if method == "GET":
            if self.raw is not None:
                return self.list_status, self.raw
            return self.list_status, json.dumps({"results": self.children}).encode()
        return self.write_status, b"{}"


def _notion(transport, token="tok"):
    return pub.NotionPublisher(PAGE, token_reader=lambda: token, transport=transport)


def test_notion_overwrites_the_existing_code_block():
    fake = FakeNotion(children=[{"id": "para", "type": "paragraph"}, {"id": "blk", "type": "code"}])
    result = _notion(fake).publish(_doc())
    assert result.ok
    method, url, headers, body = fake.calls[-1]
    assert (method, url) == ("PATCH", f"{pub.NOTION_API}/blocks/blk")
    assert headers["Notion-Version"] == pub.NOTION_VERSION
    text = "".join(part["text"]["content"] for part in body["code"]["rich_text"])
    assert json.loads(text)["slots"]["1230"]["slot"] == "1230"


def test_notion_appends_a_code_block_when_the_page_has_none():
    fake = FakeNotion()
    assert _notion(fake).publish(_doc()).ok
    method, url, _, body = fake.calls[-1]
    assert (method, url) == ("PATCH", f"{pub.NOTION_API}/blocks/{PAGE}/children")
    assert body["children"][0]["type"] == "code"


def test_notion_splits_long_payloads_into_2000_char_chunks():
    tickers = tuple(f"T{i:03d}" for i in range(60))
    fake = FakeNotion(children=[{"id": "blk", "type": "code"}])
    assert _notion(fake).publish(_doc(tickers=tickers)).ok
    parts = fake.calls[-1][3]["code"]["rich_text"]
    assert len(parts) > 1 and all(len(p["text"]["content"]) <= 2000 for p in parts)


def test_notion_refuses_a_payload_too_large_for_one_block(monkeypatch):
    monkeypatch.setattr(pub, "_NOTION_MAX_CHUNKS", 0)
    fake = FakeNotion()
    assert _notion(fake).publish(_doc()).detail == "payload exceeds one Notion code block"
    assert fake.calls == []


@pytest.mark.parametrize("reader", [lambda: None, lambda: (_ for _ in ()).throw(OSError("x"))])
def test_notion_without_token_does_not_call_the_api(reader):
    fake = FakeNotion()
    result = pub.NotionPublisher(PAGE, token_reader=reader, transport=fake).publish(_doc())
    assert (result.ok, result.detail) == (False, "notion token missing from Keychain")
    assert fake.calls == []


@pytest.mark.parametrize("fake,detail", [
    (FakeNotion(list_status=401), "list children failed (401)"),
    (FakeNotion(raw=b"not json"), "list children failed (200)"),
    (FakeNotion(children=[{"id": "blk", "type": "code"}], write_status=400), "write failed (400)"),
])
def test_notion_failures_are_sanitized(fake, detail):
    result = _notion(fake, token="tok_SECRET").publish(_doc())
    assert (result.ok, result.detail) == (False, detail)
    assert "tok_SECRET" not in result.detail


def test_notion_transport_error_is_a_failed_result():
    def transport(*a):
        raise OSError("transport failed (URLError)")

    assert _notion(transport).publish(_doc()).detail == "transport failed (URLError)"


def test_notion_rejects_bad_page_id():
    with pytest.raises(ValueError):
        pub.NotionPublisher("not-a-page")


@pytest.mark.parametrize("bad_id", ["../pages/other", "blk/children", "blk?foo=1", "blk#frag", "", None, 123])
def test_notion_rejects_unsafe_remote_block_id(bad_id):
    child = {"type": "code"} if bad_id is None else {"id": bad_id, "type": "code"}
    fake = FakeNotion(children=[child])
    result = _notion(fake).publish(_doc())
    assert (result.ok, result.detail) == (False, "list children returned invalid block id")
    assert [call[0] for call in fake.calls] == ["GET"]


def test_notion_default_token_reader_uses_keychain(monkeypatch):
    monkeypatch.setattr(pub, "keychain_secret", lambda service, account: f"{service}/{account}")
    assert pub.NotionPublisher(PAGE).token_reader() == "all-stock-dca.notion/integration"


# --- urllib transport ----------------------------------------------------------------------


class _Resp:
    status = 200

    def read(self, n=-1):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_urllib_transport_success_http_error_and_fault(monkeypatch):
    monkeypatch.setattr(dca_net_utils, "resolve_ips", lambda host: {ipaddress.ip_address("93.184.216.34")})
    monkeypatch.setattr(
        pub.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda req, timeout: _Resp()),
    )
    assert pub.urllib_transport("GET", "https://x.test", {}, b"") == (200, b"{}")

    def http_error(req, timeout):
        raise pub.urllib.error.HTTPError("https://x.test", 403, "no", {}, None)

    monkeypatch.setattr(
        pub.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=http_error),
    )
    assert pub.urllib_transport("GET", "https://x.test", {}, b"") == (403, b"")

    def boom(req, timeout):
        raise ConnectionResetError("Authorization: Bearer leaked")

    monkeypatch.setattr(
        pub.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=boom),
    )
    with pytest.raises(OSError) as info:
        pub.urllib_transport("GET", "https://x.test", {}, b"")
    assert "leaked" not in str(info.value)

    for unsafe_url in ("file:///etc/passwd", "http://x.test/v1", "ftp://x.test/resource"):
        with pytest.raises(OSError, match="UnsafeURL"):
            pub.urllib_transport("GET", unsafe_url, {}, b"")


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://127.0.0.1/v1/blocks",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/v1/blocks",
        "https://10.0.0.5/internal",
        "https://100.64.0.1/cgnat",
    ],
)
def test_urllib_transport_blocks_ssrf_private_literal_destinations(monkeypatch, unsafe_url):
    opened = []
    monkeypatch.setattr(
        pub.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda req, timeout: opened.append(req)),
    )
    monkeypatch.setattr(pub.urllib.request, "urlopen", lambda req, timeout: opened.append(req))
    with pytest.raises(OSError, match="UnsafeURL"):
        pub.urllib_transport("GET", unsafe_url, {"Authorization": "Bearer tok"}, b"")
    assert opened == []


def test_urllib_transport_blocks_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(dca_net_utils, "resolve_ips", lambda host: {ipaddress.ip_address("10.0.0.1")})
    opened = []
    monkeypatch.setattr(
        pub.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda req, timeout: opened.append(req)),
    )
    monkeypatch.setattr(pub.urllib.request, "urlopen", lambda req, timeout: opened.append(req))
    with pytest.raises(OSError, match="UnsafeURL"):
        pub.urllib_transport("GET", "https://rebind.example/v1", {"Authorization": "Bearer tok"}, b"")
    assert opened == []


def test_urllib_transport_uses_pinned_no_redirect_opener_and_refuses_redirects(monkeypatch):
    monkeypatch.setattr(dca_net_utils, "resolve_ips", lambda host: {ipaddress.ip_address("93.184.216.34")})
    seen_handlers = []
    real_build_opener = pub.urllib.request.build_opener

    class _RedirectHTTPS(pub.urllib.request.HTTPSHandler):
        def https_open(self, req):
            buf = io.BytesIO(b"moved")
            buf.msg = "Found"
            hdrs = email.message.Message()
            hdrs["Location"] = "https://attacker.example/steal"
            return urllib.response.addinfourl(buf, hdrs, req.full_url, 302)

    def spy_build_opener(*handlers):
        seen_handlers.extend(handlers)
        return real_build_opener(handlers[0], _RedirectHTTPS)

    monkeypatch.setattr(pub.urllib.request, "build_opener", spy_build_opener)
    status, body = pub.urllib_transport(
        "GET", "https://api.notion.com/v1/blocks/x", {"Authorization": "Bearer secret"}, b""
    )
    assert (status, body) == (302, b"")
    assert dca_net_utils.PinnedHTTPHandler in seen_handlers
    assert dca_net_utils.PinnedHTTPSHandler in seen_handlers
    assert dca_net_utils.BlockedFileHandler in seen_handlers
    assert dca_net_utils.BlockedFTPHandler in seen_handlers


def test_urllib_transport_rejects_oversized_response(monkeypatch):
    monkeypatch.setattr(dca_net_utils, "resolve_ips", lambda host: {ipaddress.ip_address("93.184.216.34")})
    monkeypatch.setattr(pub, "_MAX_RESPONSE_BYTES", 8)

    class _HugeResp:
        status = 200

        def read(self, n=-1):
            return b"x" * 9

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        pub.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda req, timeout: _HugeResp()),
    )
    with pytest.raises(OSError, match="ResponseTooLarge"):
        pub.urllib_transport("GET", "https://api.notion.com/v1/blocks/x", {}, b"")



# --- github branch -------------------------------------------------------------------------


class FakeGit:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, argv, input=None, env=None, **kw):
        step = argv[3]
        self.calls.append((step, argv, input, env))
        if step == self.fail_on:
            return SimpleNamespace(returncode=1, stdout="", stderr="https://user:ghp_TOKEN@github.com rejected")
        out = {"hash-object": "blobsha", "mktree": "treesha", "commit-tree": "commitsha1234567"}.get(step, "")
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")


def test_github_branch_uses_plumbing_only_and_force_pushes_an_orphan(tmp_path):
    git = FakeGit()
    result = pub.GitHubBranchPublisher(tmp_path, runner=git).publish(_doc())
    assert result.ok and result.detail == "commitsha123"
    steps = [call[0] for call in git.calls]
    assert steps == ["hash-object", "mktree", "commit-tree", "push"]
    assert json.loads(git.calls[0][2])["slots"]["1230"]["slot"] == "1230"
    assert git.calls[1][2] == f"{pub._REGULAR_FILE_MODE} blob blobsha\trecommendations.json\n"
    assert pub._REGULAR_FILE_MODE == "10" + "0644"
    commit_argv = git.calls[2][1]
    assert "-p" not in commit_argv  # orphan: no parent, so the branch never grows history
    assert git.calls[2][3]["GIT_AUTHOR_NAME"] == "all-stock-dca"
    assert git.calls[3][1][-1] == "commitsha1234567:refs/heads/recommendations"
    for forbidden in ("checkout", "stash", "add", "reset"):
        assert forbidden not in steps


def test_github_branch_failure_hides_remote_url_and_token(tmp_path):
    result = pub.GitHubBranchPublisher(tmp_path, runner=FakeGit(fail_on="push")).publish(_doc())
    assert (result.ok, result.detail) == (False, "git push failed")


@pytest.mark.parametrize("branch", ["main", "master", "claude/data", "bad branch", "x;y"])
def test_github_branch_refuses_ci_triggering_or_unsafe_branches(tmp_path, branch):
    with pytest.raises(ValueError):
        pub.GitHubBranchPublisher(tmp_path, branch=branch)


def test_github_branch_refuses_unsafe_remote(tmp_path):
    with pytest.raises(ValueError):
        pub.GitHubBranchPublisher(tmp_path, remote="origin; rm")


def test_github_branch_against_a_real_bare_remote(tmp_path):
    # End-to-end with real git and a local bare repo: the branch holds exactly one file, one commit.
    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git("init", "--bare", str(remote))
    git("init", str(work))
    git("remote", "add", "origin", str(remote), cwd=work)
    publisher = pub.GitHubBranchPublisher(work)
    assert publisher.publish(_doc()).ok
    assert publisher.publish(_doc(tickers=("MSFT",))).ok
    assert git("rev-list", "--count", "recommendations", cwd=remote) == "1"
    assert git("ls-tree", "--name-only", "recommendations", cwd=remote) == "recommendations.json"
    shown = json.loads(git("show", "recommendations:recommendations.json", cwd=remote))
    assert shown["slots"]["1230"]["items"][0]["ticker"] == "MSFT"
    assert git("status", "--porcelain", cwd=work) == ""  # working tree untouched


def test_github_branch_default_file_name_is_unchanged(tmp_path):
    assert pub.GitHubBranchPublisher(tmp_path).filename == "recommendations.json"


def test_github_branch_can_publish_another_file_name(tmp_path):
    # The slot-status signal (scripts/slot_status.py) reuses this publisher on its own branch and file.
    git = FakeGit()
    publisher = pub.GitHubBranchPublisher(tmp_path, branch="slot-status", filename="slot_status.json", runner=git)
    assert publisher.publish({"trading_day": "2026-10-29"}).ok
    assert git.calls[1][2] == f"{pub._REGULAR_FILE_MODE} blob blobsha\tslot_status.json\n"
    assert git.calls[3][1][-1] == "commitsha1234567:refs/heads/slot-status"


@pytest.mark.parametrize("filename", ["", "a/b.json", "../x.json", "x.txt", ".json", "bad name.json"])
def test_github_branch_refuses_an_unsafe_file_name(tmp_path, filename):
    with pytest.raises(ValueError):
        pub.GitHubBranchPublisher(tmp_path, filename=filename)


def test_two_branches_on_one_remote_never_clobber_each_other(tmp_path):
    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git("init", "--bare", str(remote))
    git("init", str(work))
    git("remote", "add", "origin", str(remote), cwd=work)
    assert pub.GitHubBranchPublisher(work).publish(_doc()).ok
    assert pub.GitHubBranchPublisher(work, branch="slot-status", filename="slot_status.json").publish(
        {"trading_day": "2026-10-29"}).ok
    assert git("ls-tree", "--name-only", "recommendations", cwd=remote) == "recommendations.json"
    assert git("ls-tree", "--name-only", "slot-status", cwd=remote) == "slot_status.json"


# --- settings + fan-out --------------------------------------------------------------------


def test_publishers_from_settings_respects_enabled_flags(tmp_path):
    off = {"notion": {"enabled": False, "page_id": None},
           "github_branch": {"enabled": False, "branch": "recommendations", "remote": "origin"}}
    assert pub.publishers_from_settings(off, repo_dir=tmp_path) == []
    on = {"notion": {"enabled": True, "page_id": PAGE},
          "github_branch": {"enabled": True, "branch": "recommendations", "remote": "origin"}}
    assert [p.channel for p in pub.publishers_from_settings(on, repo_dir=tmp_path)] == ["notion", "github_branch"]


def test_publish_all_isolates_a_crashing_publisher():
    class Boom:
        channel = "boom"

        def publish(self, document):
            raise RuntimeError("bug")

    class Fine:
        channel = "fine"

        def publish(self, document):
            return pub.PublishResult("fine", True)

    results = pub.publish_all([Boom(), Fine()], _doc())
    assert [(r.channel, r.ok, r.detail) for r in results] == [("boom", False, "RuntimeError"), ("fine", True, "")]
