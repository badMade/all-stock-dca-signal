"""Mirror the agent-facing recommendation payload to places a cloud agent can read.

Perplexity runs in the cloud and cannot see this Mac. The operator chose both channels on
2026-10-13:

* **Notion**: one code block on one private page, overwritten every cycle.
* **GitHub data branch**: one orphan commit, force-pushed to ``recommendations`` in the
  private repo. CI ignores branches outside ``main`` and ``claude/**``.

What is published is ONLY ``RecommendationSet.to_json()`` per slot: ticker, usd and ref_id.
No price ever leaves the Mac; ``expected_*.json`` stays local. A publish failure returns a
sanitized error and NEVER blocks or undoes the local write, because the local file is what
the Claude agent reads.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from dca_rec_models import SLOT_IDS, RecommendationSet

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# Notion caps one rich-text object at 2,000 characters and a block at 100 of them.
_NOTION_CHUNK = 2000
_NOTION_MAX_CHUNKS = 100
_KEYCHAIN_SERVICE = re.compile(r"^[A-Za-z0-9._-]+$")
# git's regular-file tree mode, written as an octal literal: the bare six-digit string trips the
# account-number invariant (tests/test_config_invariants.py), which scans every tracked file.
_REGULAR_FILE_MODE = f"{0o100644:o}"


@dataclass(frozen=True)
class PublishResult:
    channel: str
    ok: bool
    detail: str = ""


class Publisher(Protocol):
    channel: str

    def publish(self, document: Mapping[str, Any]) -> PublishResult: ...


def build_document(sets: Mapping[str, RecommendationSet], *, generated_at: datetime) -> dict[str, Any]:
    """The one payload every channel carries: the newest set per slot, agent-safe fields only."""
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    unknown = set(sets) - set(SLOT_IDS)
    if unknown:
        raise ValueError(f"unknown slot(s): {sorted(unknown)}")
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        # Readers apply the same freshness rule as the local agent: newest as_of <= now, <= 15 min old.
        "slots": {slot: sets[slot].to_json() for slot in sorted(sets)},
    }


def _canonical(document: Mapping[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)


def keychain_secret(service: str, account: str, *, runner: Callable[..., Any] = subprocess.run) -> str | None:
    """Read one generic password from the macOS Keychain; None when it is absent.

    The secret never touches argv, a file or a log line; only ``security``'s stdout carries it.
    """
    if not _KEYCHAIN_SERVICE.match(service) or not _KEYCHAIN_SERVICE.match(account):
        raise ValueError("unsafe keychain service/account name")
    result = runner(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                    check=False, capture_output=True, text=True)
    if getattr(result, "returncode", 1) != 0:
        return None
    value = str(result.stdout).strip()
    return value or None


Transport = Callable[[str, str, dict[str, str], bytes], tuple[int, bytes]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise OSError("transport failed (UnsafeURL)")
    request = urllib.request.Request(url, data=body or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - https scheme enforced above
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""  # the body can echo the request; keep only the status
    except Exception as exc:  # noqa: BLE001 - any transport fault is one sanitized publish failure
        raise OSError(f"transport failed ({type(exc).__name__})") from None


class NotionPublisher:
    """Overwrite the first code block on one Notion page (append one if the page has none)."""

    channel = "notion"

    def __init__(self, page_id: str, *, token_reader: Callable[[], str | None] | None = None,
                 transport: Transport = urllib_transport) -> None:
        if not isinstance(page_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", page_id):
            raise ValueError("notion page_id must be a 32-hex Notion id")
        self.page_id = page_id
        self.token_reader = token_reader or (lambda: keychain_secret("all-stock-dca.notion", "integration"))
        self.transport = transport

    def _call(self, method: str, path: str, token: str, payload: Any = None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                   "Content-Type": "application/json"}
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        status, raw = self.transport(method, f"{NOTION_API}{path}", headers, body)
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            return status, None

    def publish(self, document: Mapping[str, Any]) -> PublishResult:
        text = _canonical(document)
        chunks = [text[i:i + _NOTION_CHUNK] for i in range(0, len(text), _NOTION_CHUNK)]
        if len(chunks) > _NOTION_MAX_CHUNKS:
            return PublishResult(self.channel, False, "payload exceeds one Notion code block")
        try:
            token = self.token_reader()
        except (OSError, ValueError):
            token = None
        if not token:
            return PublishResult(self.channel, False, "notion token missing from Keychain")
        code = {"language": "json", "rich_text": [{"type": "text", "text": {"content": c}} for c in chunks]}
        try:
            status, listing = self._call("GET", f"/blocks/{self.page_id}/children?page_size=100", token)
            if status != 200 or not isinstance(listing, dict):
                return PublishResult(self.channel, False, f"list children failed ({status})")
            block = next((b for b in listing.get("results") or [] if isinstance(b, dict) and b.get("type") == "code"),
                         None)
            if block is None:
                status, _ = self._call("PATCH", f"/blocks/{self.page_id}/children", token,
                                       {"children": [{"object": "block", "type": "code", "code": code}]})
            else:
                status, _ = self._call("PATCH", f"/blocks/{block['id']}", token, {"code": code})
        except OSError as exc:
            return PublishResult(self.channel, False, str(exc))
        if status != 200:
            return PublishResult(self.channel, False, f"write failed ({status})")
        return PublishResult(self.channel, True, f"{len(text)} chars")


class GitHubBranchPublisher:
    """Force-push one orphan commit holding one JSON file (``recommendations.json`` by default) to a data-only branch.

    Plumbing only (hash-object, mktree, commit-tree, push). It never checks out, stashes or
    touches the working tree or index, because the operator commits from this same checkout
    mid-cycle.
    """

    channel = "github_branch"

    def __init__(self, repo_dir: str | Path, *, branch: str = "recommendations", remote: str = "origin",
                 filename: str = "recommendations.json", runner: Callable[..., Any] = subprocess.run) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch in {"main", "master"} or branch.startswith("claude/"):
            # main and claude/** trigger CI on push (.github/workflows/ci.yml); a data branch must not.
            raise ValueError("branch must be a plain data branch outside main and claude/**")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
            raise ValueError("unsafe remote name")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename):
            # Goes into a mktree line verbatim: one plain file name, no path, no whitespace (2026-10-30, the
            # slot-status signal reuses this publisher with its own file on its own branch).
            raise ValueError("filename must be one plain *.json name")
        self.repo_dir = Path(repo_dir)
        self.branch = branch
        self.remote = remote
        self.filename = filename
        self.runner = runner

    def _git(self, *args: str, stdin: str | None = None, env: dict[str, str] | None = None) -> str:
        result = self.runner(["git", "-C", str(self.repo_dir), *args], input=stdin, capture_output=True, text=True,
                             check=False, env=env)
        if getattr(result, "returncode", 1) != 0:
            # stderr can name the remote URL (and a token embedded in it); report the step only.
            raise OSError(f"git {args[0]} failed")
        return str(result.stdout).strip()

    def publish(self, document: Mapping[str, Any]) -> PublishResult:
        try:
            blob = self._git("hash-object", "-w", "--stdin", stdin=_canonical(document) + "\n")
            tree = self._git("mktree", stdin=f"{_REGULAR_FILE_MODE} blob {blob}\t{self.filename}\n")
            env = {**os.environ, "GIT_AUTHOR_NAME": "all-stock-dca", "GIT_AUTHOR_EMAIL": "noreply@all-stock-dca",
                   "GIT_COMMITTER_NAME": "all-stock-dca", "GIT_COMMITTER_EMAIL": "noreply@all-stock-dca"}
            stamp = document.get("generated_at", document.get("updated_at", ""))
            commit = self._git("commit-tree", tree, "-m", f"{self.filename[:-5]} {stamp}",
                               env=env)
            self._git("push", "--force", "--quiet", self.remote, f"{commit}:refs/heads/{self.branch}")
        except OSError as exc:
            return PublishResult(self.channel, False, str(exc))
        return PublishResult(self.channel, True, commit[:12])


def publishers_from_settings(publish: Mapping[str, Any], *, repo_dir: str | Path) -> list[Publisher]:
    """The enabled publishers named by ``config/recommendations.json``'s ``publish`` block."""
    result: list[Publisher] = []
    notion = publish.get("notion") or {}
    if notion.get("enabled"):
        result.append(NotionPublisher(notion.get("page_id")))
    github = publish.get("github_branch") or {}
    if github.get("enabled"):
        result.append(GitHubBranchPublisher(repo_dir, branch=github.get("branch", "recommendations"),
                                            remote=github.get("remote", "origin")))
    return result


def publish_all(publishers: list[Publisher], document: Mapping[str, Any]) -> list[PublishResult]:
    """Run every publisher; one channel's failure never stops the others."""
    results = []
    for publisher in publishers:
        try:
            results.append(publisher.publish(document))
        except Exception as exc:  # noqa: BLE001 - a publisher bug must not break the cycle
            results.append(PublishResult(getattr(publisher, "channel", "unknown"), False, type(exc).__name__))
    return results
