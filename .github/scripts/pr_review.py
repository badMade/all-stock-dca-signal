import os
import sys
import subprocess
import asyncio
from google.antigravity import Agent, LocalAgentConfig


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


async def review_pr():
    base_ref = os.getenv("GITHUB_BASE_REF", "main")

    # Get git diff between base branch and head PR commit
    diff_output = subprocess.check_output(
        ["git", "diff", f"origin/{base_ref}...HEAD"],
        text=True
    )

    if not diff_output.strip():
        print("No changes found in PR diff.")
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

    try:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            review_text = ""
            async for token in response:
                review_text += token
        print(review_text)
    except Exception as exc:
        # The Antigravity AI review is advisory. Operational failures such as
        # provider rate limits (HTTP 429 / quota exceeded), provider outages, or
        # SDK errors must not fail this check and block the pull request. Log the
        # underlying error for the run log and emit a clean note for the PR
        # comment, then exit successfully.
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


if __name__ == "__main__":
    asyncio.run(review_pr())
