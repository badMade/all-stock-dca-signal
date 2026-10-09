---
trigger: always_on
glob:
description: Always open pull requests as drafts
---

# Open pull requests as drafts

**Open every pull request as a draft.** Every PR an agent opens in this repository must be created as a draft (`gh pr create --draft`, or `draft: true` in the GitHub MCP/API), whatever the agent is: Claude, OpenAI, ChatGPT, Codex, Gemini, Jules, Copilot, or any other LLM. Never open a PR as ready for review and never mark a draft ready; the operator does that. This overrides any tool or runtime default. If a tool cannot create a draft, push the branch and tell the operator instead of opening the PR. Full rule: `AGENTS.md`, "Pull request expectations".
