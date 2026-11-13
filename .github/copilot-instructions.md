# Copilot code review instructions

Review the pull request diff for concrete defects or regressions introduced by the change. Report only actionable findings. For each finding, identify the affected location, describe a plausible failure scenario and impact, and suggest a practical fix. Avoid speculation, style-only feedback, duplicate findings, and unrelated issues. Do not claim checks passed unless you ran them.

Prioritize these repository-specific risks:

- **Untrusted or persisted input:** reject booleans where numbers are expected, non-finite floats, duplicate JSON keys when values carry safety bounds, and ambiguous blank or missing values. Treat non-object payloads and incomplete or unreadable artifacts as unavailable rather than valid empty data.
- **Evidence and freshness:** fail closed when required evidence is missing, stale, incomplete, or unclassifiable. Check that safety decisions use fresh snapshots and clocks, and that absent history is not silently treated as zero or complete.
- **State transitions:** check retry safety, atomicity, ownership, and idempotency for releases, settlements, and persisted state changes.
- **Network destinations and errors:** route operator-supplied URLs through `dca_net_utils.assert_safe_destination`. Preserve existing `# noqa: S310` suppressions when editing nearby code. Never log raw external error responses, which may echo credentials.
- **Financial execution paths:** distinguish candidates, simulated orders, submitted orders, and confirmed fills. `run_dca.run_slot()`, the `MorningDCAExecutor` hierarchy, and `dca_runner.py` are separate entry points; verify which paths the change covers and flag likely gaps where behavior should be consistent.
- **Coverage boundaries:** consult `AGENTS.md` for companion packaging/config updates, excluded subproject checks, and other repository-specific validation gaps. Do not assume root CI covers excluded projects: each sibling subproject has its own CI job, and none of them runs on draft PRs.
- **Secrets and operational artifacts:** flag credentials, account identifiers, or `.env` contents added to tracked files. Treat generated operational reports and protected data files as read-only unless the change explicitly requires otherwise.

Never approve or merge a pull request. Do not recommend editing `.github/workflows/`; this repository prohibits agent-driven PRs from changing workflow files. Describe any workflow concern as a proposed change instead.

**Open every pull request as a draft.** Every PR an agent opens (the Copilot coding agent included) in this repository must be created as a draft (`gh pr create --draft`, or `draft: true` in the GitHub MCP/API), whatever the agent is: Claude, OpenAI, ChatGPT, Codex, Gemini, Jules, Copilot, or any other LLM. Never open a PR as ready for review and never mark a draft ready; the operator does that. This overrides any tool or runtime default. If a tool cannot create a draft, push the branch and tell the operator instead of opening the PR. Full rule: `AGENTS.md`, "Pull request expectations".
