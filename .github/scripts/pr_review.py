import os
import subprocess
import asyncio
from google.antigravity import Agent, LocalAgentConfig

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

    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        review_text = ""
        async for token in response:
            review_text += token
        print(review_text)

if __name__ == "__main__":
    asyncio.run(review_pr())
