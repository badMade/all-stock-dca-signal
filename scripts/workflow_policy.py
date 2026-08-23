#!/usr/bin/env python3
"""Static policy checks over ``.github/workflows/*.yml``.

Three org-wide policies, each with a documented rationale and an explicit
escape hatch:

  * every job sets ``timeout-minutes`` -- a hung job with no ceiling burns the
    full 6-hour runner default before Actions kills it, and for a privileged
    job that is an extended window holding a live secret for no reason;
  * every workflow (or job) declares a ``concurrency`` policy with a
    ``group`` and an explicit ``cancel-in-progress`` -- this repo's
    convention is that PR/branch runs cancel a superseded run and ``main``
    never does, since ``main``'s runs are the audit record of what landed;
  * every ``actions/checkout`` step sets ``persist-credentials: false`` --
    a checkout that keeps credentials leaves the token readable from git
    config to every later step and any script it runs, including, in a
    privileged job, PR-influenced tooling. The action's own default is
    ``true``; this repo's convention is the opposite.

Exceptions are declared in a workflow file's own ``x-policy-exceptions`` block,
a top-level key GitHub Actions ignores (the same trick already used for YAML
anchor blocks in shared workflows), so it is a schema-legal escape hatch
rather than a magic comment a YAML round-trip could silently drop:

    x-policy-exceptions:
      timeout-minutes:
        - job: some-job
          reason: "why this job is exempt"
      persist-credentials:
        - job: some-job
          step: "Some step name"   # optional: omit to exempt the whole job
          reason: "why this checkout is exempt"

Wired into pytest (scripts/tests/test_workflow_policy.py) rather than a
separate CI job, consistent with how test_config_invariants.py enforces
config invariants as ordinary tests that already run on every push.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import yaml  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


class Violation(NamedTuple):
    file: str
    job: Optional[str]
    step: Optional[str]
    message: str

    def __str__(self) -> str:
        loc = self.file
        if self.job:
            loc += f" :: job '{self.job}'"
        if self.step:
            loc += f" :: step '{self.step}'"
        return f"{loc}: {self.message}"


class PolicyException(NamedTuple):
    job: str
    step: Optional[str]  # None means the whole job is exempt
    reason: str


def _normalize_on_key(doc: dict) -> dict:
    """Rewrite the YAML-1.1 boolean key ``True`` back to the string ``"on"``.

    PyYAML's default (``safe_load``) resolver treats the bare scalar ``on`` as
    the YAML 1.1 boolean ``true`` -- so a workflow's top-level ``on:`` key
    parses as the Python key ``True``, not the string ``"on"``, and
    ``doc["on"]`` raises ``KeyError`` even though the file is completely
    ordinary GitHub Actions YAML. Every workflow file trips this, so it is
    normalized once here rather than by every caller remembering the gotcha.
    """
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


def load_workflows(directory: Path = WORKFLOWS_DIR) -> Dict[str, dict]:
    """Every workflow file, parsed. Keyed by path relative to the repo root.

    GitHub Actions accepts both ``.yml`` and ``.yaml`` for workflow files, so a
    checker that only globbed the first would stay green forever on a workflow
    someone added with the other extension -- every policy this module
    enforces would then simply never see it (Codex review, PR #219).
    """
    out: Dict[str, dict] = {}
    paths = sorted(set(directory.glob("*.yml")) | set(directory.glob("*.yaml")))
    for path in paths:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[str(path.relative_to(REPO_ROOT))] = _normalize_on_key(doc)
    return out


def _jobs(doc: dict) -> Dict[str, dict]:
    return doc.get("jobs") or {}


def _steps(job: dict) -> List[dict]:
    return job.get("steps") or []


def _exceptions_for(doc: dict, policy: str) -> List[PolicyException]:
    entries = (doc.get("x-policy-exceptions") or {}).get(policy) or []
    out: List[PolicyException] = []
    for entry in entries:
        job = entry.get("job")
        reason = entry.get("reason")
        if not job or not reason:
            raise ValueError(
                f"x-policy-exceptions.{policy} entries need both 'job' and "
                f"'reason': {entry!r}"
            )
        out.append(PolicyException(job, entry.get("step"), reason))
    return out


def _is_exempt(exceptions: List[PolicyException], job_id: str, step_name: Optional[str] = None) -> bool:
    for exc in exceptions:
        if exc.job != job_id:
            continue
        if exc.step is None or exc.step == step_name:
            return True
    return False


def check_timeout_minutes(docs: Optional[Dict[str, dict]] = None) -> List[Violation]:
    docs = load_workflows() if docs is None else docs
    violations: List[Violation] = []
    for file, doc in docs.items():
        exceptions = _exceptions_for(doc, "timeout-minutes")
        for job_id, job in _jobs(doc).items():
            if "timeout-minutes" in job:
                continue
            if _is_exempt(exceptions, job_id):
                continue
            violations.append(Violation(file, job_id, None, "missing timeout-minutes"))
    return violations


_FALSE_ISH = {False, "false"}


def check_concurrency(docs: Optional[Dict[str, dict]] = None) -> List[Violation]:
    docs = load_workflows() if docs is None else docs
    violations: List[Violation] = []
    for file, doc in docs.items():
        exceptions = _exceptions_for(doc, "concurrency")
        wf_concurrency = doc.get("concurrency")
        for job_id, job in _jobs(doc).items():
            if _is_exempt(exceptions, job_id):
                continue
            concurrency = job.get("concurrency", wf_concurrency)
            if concurrency is None:
                violations.append(
                    Violation(file, job_id, None, "no concurrency policy (workflow- or job-level)")
                )
                continue
            if not isinstance(concurrency, dict) or "group" not in concurrency:
                violations.append(
                    Violation(file, job_id, None, f"concurrency block missing 'group': {concurrency!r}")
                )
                continue
            if "cancel-in-progress" not in concurrency:
                violations.append(
                    Violation(file, job_id, None, "concurrency block missing 'cancel-in-progress'")
                )
    return violations


def check_persist_credentials(docs: Optional[Dict[str, dict]] = None) -> List[Violation]:
    docs = load_workflows() if docs is None else docs
    violations: List[Violation] = []
    for file, doc in docs.items():
        exceptions = _exceptions_for(doc, "persist-credentials")
        for job_id, job in _jobs(doc).items():
            for step in _steps(job):
                uses = str(step.get("uses", ""))
                if not uses.startswith("actions/checkout@"):
                    continue
                step_name = step.get("name", uses)
                if _is_exempt(exceptions, job_id, step_name):
                    continue
                with_block = step.get("with") or {}
                value = with_block.get("persist-credentials")
                if value not in _FALSE_ISH:
                    violations.append(
                        Violation(
                            file, job_id, step_name, f"persist-credentials is {value!r}, must be false"
                        )
                    )
    return violations


ALL_CHECKS = (check_timeout_minutes, check_concurrency, check_persist_credentials)


def format_violations(violations: List[Violation]) -> str:
    return "\n".join(f"  - {v}" for v in violations)


def _main() -> int:  # pragma: no cover - manual/CLI convenience only
    failed = False
    for check in ALL_CHECKS:
        violations = check()
        if violations:
            failed = True
            print(f"{check.__name__}: {len(violations)} violation(s)")
            print(format_violations(violations))
    if not failed:
        print("All workflow policy checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main())
