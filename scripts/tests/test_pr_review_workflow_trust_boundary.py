"""Guardrail tests for the two-workflow PR review split's trust boundary.

antigravity-pr-review.yml (unprivileged, `pull_request`-triggered) computes a
diff and uploads it as an artifact; it holds no secret and must never gain
one. antigravity-pr-review-post.yml (privileged, `workflow_run`-triggered)
holds GEMINI_API_KEY and pull-requests: write, and is the only place a PR's
influence must never become code execution.

These tests encode the specific structural properties the split relies on --
see antigravity-pr-review-post.yml's header comment for the design this
verifies. Each test fails with a message naming exactly what drifted, because
a guardrail that fails opaquely gets bypassed by a future editor rather than
understood.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from scripts.workflow_policy import load_workflows  # noqa: E402
except ImportError:  # pragma: no cover - fallback when running directly in scripts/
    from workflow_policy import load_workflows  # type: ignore # noqa: E402

ANALYZE_FILE = ".github/workflows/antigravity-pr-review.yml"
POST_FILE = ".github/workflows/antigravity-pr-review-post.yml"

DOCS = load_workflows()
ANALYZE = DOCS[ANALYZE_FILE]
POST = DOCS[POST_FILE]


def _all_steps(doc):
    for job_id, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_id, step


def _job(doc, job_id):
    return (doc.get("jobs") or {})[job_id]


# --- The privileged workflow never checks out PR-head-controlled code --------

def test_no_checkout_step_in_the_privileged_workflow_sets_a_ref():
    """Omitting `ref:` on actions/checkout in a workflow_run job means
    "check out github.sha", which IS the default branch for that event --
    never the branch that triggered it. Any `ref:` at all here is a red flag,
    not just one that happens to reference PR-head fields, because the whole
    point of this job is that it never needs to name a ref."""
    offenders = []
    for job_id, step in _all_steps(POST):
        if not str(step.get("uses", "")).startswith("actions/checkout@"):
            continue
        with_block = step.get("with") or {}
        if "ref" in with_block:
            offenders.append(f"{job_id} :: {step.get('name')} :: ref={with_block['ref']!r}")
    assert not offenders, "\n".join(offenders)


def test_the_privileged_workflow_never_references_github_token_secret_for_pr_head_checkout():
    """A checkout with a token but no ref override is fine (that's just auth);
    this specifically guards against a future edit combining a PR-head ref
    with elevated checkout permissions."""
    for job_id, step in _all_steps(POST):
        if not str(step.get("uses", "")).startswith("actions/checkout@"):
            continue
        with_block = step.get("with") or {}
        assert "ref" not in with_block, f"{job_id} :: {step.get('name')}"


# --- The review job cannot run before provenance is validated -----------------

def test_review_job_structurally_depends_on_validate_provenance():
    assert "validate-provenance" in POST["jobs"], "expected a validate-provenance job"
    assert "review" in POST["jobs"], "expected a review job"
    needs = _job(POST, "review").get("needs")
    needs_list = [needs] if isinstance(needs, str) else (needs or [])
    assert "validate-provenance" in needs_list, (
        f"review job's needs={needs_list!r} does not include validate-provenance"
    )


def test_validate_provenance_job_gates_on_the_triggering_run_succeeding():
    job = _job(POST, "validate-provenance")
    assert job.get("if") == "github.event.workflow_run.conclusion == 'success'", job.get("if")


def test_validate_provenance_checks_the_required_fields():
    """Smoke check that the validation script actually inspects what its name
    promises, not just that a step with this name exists."""
    job = _job(POST, "validate-provenance")
    script = ""
    for step in job.get("steps") or []:
        with_block = step.get("with") or {}
        if "script" in with_block:
            script += with_block["script"]
    required_substrings = [
        "run.name",  # workflow identity
        "run.event",  # triggering event type
        "run.conclusion",  # the triggering run actually succeeded
        "head_repository",  # same-repo, not a fork
        "pull_requests",  # PR linkage
        "core.setFailed",  # violations actually fail the job, not just log
    ]
    missing = [s for s in required_substrings if s not in script]
    assert not missing, f"validate-provenance script is missing checks for: {missing}"


def test_validate_provenance_outputs_feed_the_review_job():
    """The review job must consume the VALIDATED pr_number/head_sha, not raw
    event fields -- otherwise the validation job is decorative."""
    review = _job(POST, "review")
    raw_text = str(review)
    assert "needs.validate-provenance.outputs.pr_number" in raw_text
    assert "needs.validate-provenance.outputs.head_sha" in raw_text


# --- Artifact provenance is cross-checked before it is trusted ----------------

def test_downloaded_artifact_metadata_is_cross_checked_before_use():
    review = _job(POST, "review")
    names = [s.get("name", "") for s in review.get("steps") or []]

    def index_of(substr):
        for i, name in enumerate(names):
            if substr.lower() in name.lower():
                return i
        return None

    download_i = index_of("Download review input artifact")
    crosscheck_i = index_of("Cross-check artifact metadata")
    generate_i = index_of("Generate PR Review")
    assert download_i is not None, names
    assert crosscheck_i is not None, names
    assert generate_i is not None, names
    assert download_i < crosscheck_i < generate_i, (
        f"expected download({download_i}) < cross-check({crosscheck_i}) < generate({generate_i})"
    )


def test_checkout_precedes_generation_in_the_review_job():
    review = _job(POST, "review")
    steps = review.get("steps") or []
    checkout_i = next(
        (i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout@")),
        None,
    )
    names = [s.get("name", "") for s in steps]
    generate_i = next((i for i, n in enumerate(names) if "generate pr review" in n.lower()), None)
    assert checkout_i is not None, "no checkout step found in the review job"
    assert generate_i is not None, names
    assert checkout_i < generate_i


def test_dedup_check_precedes_checkout_so_an_already_reviewed_head_does_no_work():
    review = _job(POST, "review")
    steps = review.get("steps") or []
    names = [s.get("name", "") for s in steps]
    check_i = next((i for i, n in enumerate(names) if "existing review" in n.lower()), None)
    checkout_i = next(
        (i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout@")),
        None,
    )
    assert check_i is not None and checkout_i is not None
    assert check_i < checkout_i


# --- Least privilege ------------------------------------------------------------

def test_the_analyze_workflow_holds_no_secret_and_no_write_permission():
    raw_text = str(ANALYZE)
    assert "secrets." not in raw_text, "the unprivileged analyzer must never reference a secret"
    top_perms = ANALYZE.get("permissions") or {}
    assert top_perms == {"contents": "read"}, top_perms
    for job_id, job in ANALYZE["jobs"].items():
        job_perms = job.get("permissions", top_perms)
        assert job_perms == {"contents": "read"}, f"{job_id}: {job_perms}"


def test_validate_provenance_job_needs_no_elevated_permissions():
    """It only reads context.payload fields already delivered with the event;
    it makes no API calls, so it needs nothing beyond the workflow-level
    read-only default."""
    job = _job(POST, "validate-provenance")
    assert "permissions" not in job or job["permissions"] == {"contents": "read"}


def test_review_job_permissions_are_exactly_what_it_uses():
    job = _job(POST, "review")
    perms = job.get("permissions") or {}
    assert perms == {"contents": "read", "pull-requests": "write", "actions": "read"}, perms


# --- Cross-file wiring: a rename in one file must not silently break the other -

def test_the_post_workflow_triggers_on_the_analyze_workflows_actual_name():
    trigger_names = POST["on"]["workflow_run"]["workflows"]
    assert ANALYZE["name"] in trigger_names, (
        f"analyze workflow is named {ANALYZE['name']!r} but the post workflow's "
        f"workflow_run trigger only matches {trigger_names!r} -- a rename of one "
        "without the other means workflow_run silently stops firing"
    )


def test_the_post_workflow_only_reacts_to_completed_runs():
    assert POST["on"]["workflow_run"]["types"] == ["completed"]


# --- The "Check for existing review" step's dedup logic ------------------------

def _check_step(review=None):
    review = review or _job(POST, "review")
    steps = review.get("steps") or []
    return next(s for s in steps if s.get("name") == "Check for existing review")


def test_the_dedup_check_only_trusts_a_marker_from_the_bot_that_posts_it():
    """A comment's body alone is attacker-controlled -- any PR commenter can
    paste the literal marker text to permanently suppress their own PR's
    review -- so the dedup check must also require the posting user's login
    to match the actor the job itself posts as (Codex review, PR #219:
    "Trust completion markers only from the review bot")."""
    script = _check_step()["with"]["script"]
    assert "github-actions[bot]" in script, (
        "the dedup check's script must pin the trusted marker author to "
        "github-actions[bot], the identity github.token posts comments as"
    )
    assert "c.user.login === TRUSTED_MARKER_AUTHOR" in script or (
        "user.login" in script and "TRUSTED_MARKER_AUTHOR" in script
    ), "the marker match must be conditioned on comment authorship, not body text alone"


def test_the_dedup_check_compares_the_current_pr_head_before_proceeding():
    """cancel-in-progress is false on the review job's concurrency group (see
    the header comment), so an older run queued behind a newer push is not
    cancelled -- it is left to run, and without this comparison it would
    generate and post a review for a diff the PR has since moved past
    (Codex review, PR #219: "Skip reviews for superseded head SHAs")."""
    script = _check_step()["with"]["script"]
    assert "pulls.get" in script, "must fetch the PR to read its current head"
    assert "pr.data.head.sha" in script and "headSha" in script, (
        "must compare the freshly-fetched head sha against the sha this run "
        "was provenance-validated for"
    )


# --- Pinned, reviewed dependency in the privileged job --------------------------

def test_the_privileged_review_job_pins_an_exact_antigravity_sdk_version():
    """Every other CI dependency in this repo is exact-pinned
    (requirements-dev.txt). This install runs with GEMINI_API_KEY in its
    environment, so an unpinned `pip install google-antigravity` would let an
    unreviewed upstream release -- compromised or merely incompatible -- run
    with that key on every PR, with no corresponding change in this repo to
    review (Codex review, PR #219: "Pin the privileged reviewer SDK")."""
    review = _job(POST, "review")
    steps = review.get("steps") or []
    install = next(s for s in steps if s.get("name") == "Install Antigravity SDK")
    run = install.get("run", "")
    match = re.search(r"pip install google-antigravity(\S*)", run)
    assert match is not None, run
    assert match.group(1).startswith("=="), (
        f"google-antigravity must be pinned with '==', found {match.group(1)!r} in: {run!r}"
    )


# --- The posting step rechecks supersession right before it acts ---------------

def test_the_post_comment_step_rechecks_the_pr_head_before_posting():
    """The "Check for existing review" step's supersession check runs before
    checkout/download/generation -- steps that can take long enough for a new
    push to land -- so it is stale by the time this step is about to post.
    Without a fresh check here, a superseded run would still attach feedback
    and a completion marker to a diff the PR has already moved past (Codex
    review, PR #219: "Recheck the PR head immediately before posting")."""
    review = _job(POST, "review")
    steps = review.get("steps") or []
    post = next(s for s in steps if s.get("name") == "Post PR Comment")
    script = post["with"]["script"]
    assert "pulls.get" in script, "must fetch the PR to read its current head"
    assert "currentPr.data.head.sha" in script and "headSha" in script, (
        "must compare the freshly-fetched head sha against the sha this run "
        "was provenance-validated for, immediately before posting"
    )


# --- The base SHA is pinned and rechecked, not just the head SHA ---------------

def test_the_analyzer_diffs_against_the_pinned_base_sha_not_the_moving_branch_tip():
    """origin/${BASE_REF} is the base branch's CURRENT tip at checkout time,
    not the base this PR/review is actually reasoned about -- if the base
    branch advances between the pull_request event and this job's checkout,
    diffing against the moving ref computes a diff against a different base
    than the one recorded into metadata.json (Codex review, PR #219: "Pin and
    recheck the reviewed base SHA")."""
    analyze = _job(ANALYZE, "analyze")
    steps = analyze.get("steps") or []
    compute = next(s for s in steps if s.get("name") == "Compute diff and metadata")
    run = compute.get("run", "")
    assert '"${BASE_SHA}...HEAD"' in run, (
        f"diff must be computed against the pinned BASE_SHA, not a moving branch ref: {run!r}"
    )
    assert '"origin/${BASE_REF}...HEAD"' not in run, run


def test_validate_provenance_also_outputs_a_base_sha():
    """head_sha alone lets the review job detect a PR that moved to a new
    commit, but not a base branch that advanced out from under an unchanged
    head -- the diff would then describe a base revision the PR is no longer
    actually based on (Codex review, PR #219: "Pin and recheck the reviewed
    base SHA")."""
    job = _job(POST, "validate-provenance")
    assert "base_sha" in (job.get("outputs") or {}), job.get("outputs")
    script = ""
    for step in job.get("steps") or []:
        with_block = step.get("with") or {}
        if "script" in with_block:
            script += with_block["script"]
    assert "pr.base.sha" in script, "the provenance step must read pr.base.sha"
    assert 'core.setOutput("base_sha"' in script


def test_the_dedup_check_also_compares_the_current_base_sha():
    script = _check_step()["with"]["script"]
    assert "pr.data.base.sha" in script and "baseSha" in script, (
        "must compare the freshly-fetched base sha against the sha this run "
        "was provenance-validated for, the same way head sha is compared"
    )


def test_the_post_comment_step_also_rechecks_the_base_sha():
    review = _job(POST, "review")
    steps = review.get("steps") or []
    post = next(s for s in steps if s.get("name") == "Post PR Comment")
    script = post["with"]["script"]
    assert "currentPr.data.base.sha" in script and "baseSha" in script, (
        "must compare the freshly-fetched base sha against the sha this run "
        "was provenance-validated for, immediately before posting"
    )


def test_the_artifact_metadata_crosscheck_also_covers_the_base_sha():
    review = _job(POST, "review")
    steps = review.get("steps") or []
    crosscheck = next(s for s in steps if s.get("name") == "Cross-check artifact metadata against provenance")
    run = crosscheck.get("run", "")
    assert "EXPECTED_BASE_SHA" in str(crosscheck.get("env") or {})
    assert 'meta["base_sha"]' in run
