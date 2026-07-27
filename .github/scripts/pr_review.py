import asyncio
import os
import sys
import subprocess


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if the exception looks like a provider rate-limit/quota error."""
    text = str(exc).lower()
    return (
        "429" in text
        or "resource_exhausted" in text
        or "quota" in text
        or "rate limit" in text
        or "rate-limit" in text
    )


def _emit_skipped_note(exc: Exception) -> None:
    """Log the underlying error and emit a clean skipped-review note (then exit 0).

    The Antigravity AI review is advisory: operational failures (a missing base
    ref, missing optional tooling, provider rate limits / outages) must not fail
    this check and block the pull request.
    """
    print(f"Antigravity PR review skipped due to error: {exc}", file=sys.stderr)
    if _is_rate_limit_error(exc):
        print(
            "> ⚠️ Automated AI review was skipped because the model "
            "provider is temporarily rate-limited (HTTP 429 / quota exceeded). "
            "This is a limitation of the review tooling and does not indicate a "
            "problem with the changes in this pull request."
        )
    else:
        print(
            "> ⚠️ Automated AI review could not be generated due to a "
            "temporary error in the review tooling. This does not indicate a "
            "problem with the changes in this pull request."
        )


async def review_pr():
    base_ref = os.getenv("GITHUB_BASE_REF", "main")

    # Diff generation. A missing base ref (git exits 128 when origin/<base> is not
    # in the checkout) or a missing git binary is an environment issue, not a code
    # problem in the PR, so skip the review rather than fail the check.
    try:
        diff_output = subprocess.check_output(
            ["git", "diff", f"origin/{base_ref}...HEAD"],
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        _emit_skipped_note(exc)
        return

    if not diff_output.strip():
        print("No changes found in PR diff.")
        return

    # Optional reviewer SDK. Only a genuinely MISSING module is the non-blocking
    # "tooling not installed" case, caught narrowly here (AGENTS.md forbids broad
    # try/except around imports). An import that fails for any other reason -- a
    # compatibility or programming error during initialization -- must surface for
    # repair rather than be silently skipped, so it is deliberately NOT caught.
    try:
        from google.antigravity import Agent, LocalAgentConfig
    except ModuleNotFoundError as exc:
        _emit_skipped_note(exc)
        return

    # Truncate diff if extremely large to fit context window
    truncated_diff = diff_output[:120000]
    prompt = f"""
Please perform a concise, high-quality code review of the following Pull Request diff:
```diff
{truncated_diff}
```

Focus your feedback on:
1. Bugs, security risks, performance issues, or edge cases.
2. Code structure, maintainability, and best practices.
3. Actionable improvements with concrete recommendations.
"""

    config = LocalAgentConfig(
        system_instructions="You are an expert senior software engineer performing automated code reviews on GitHub Pull Requests."
    )

    # Provider call. Rate limits (HTTP 429 / quota), provider outages, and provider
    # SDK runtime errors are advisory and must not block the PR: fail open with a
    # note. This broad catch is intentional and scoped to the provider call only
    # (not the import above).
    try:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            review_text = ""
            async for token in response:
                review_text += token
        print(review_text)
    except Exception as exc:  # noqa: BLE001 - provider errors are advisory; fail open
        _emit_skipped_note(exc)


if __name__ == "__main__":
    asyncio.run(review_pr())
