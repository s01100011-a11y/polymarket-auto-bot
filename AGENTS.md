# Repository contribution rules

These rules apply to ChatGPT Codex and human contributors working in this repository.

## Change workflow

- Never commit or push changes directly to `main`.
- Create a dedicated branch for every task or issue.
- Submit all code changes through a pull request targeting `main`.
- Do not merge pull requests unless the repository owner explicitly instructs you to merge.
- Keep each pull request scoped to the assigned task or GitHub issue.
- Reference the relevant GitHub issue in the pull request when one exists.

## Safety and production controls

- Do not modify secrets, credentials, API keys, wallet keys, authentication tokens, or private environment values.
- Do not commit secrets or credentials to the repository.
- Do not change Railway production variables, deployment settings, domains, or production infrastructure unless explicitly requested.
- Do not enable, disable, or alter live-money trading behavior unless explicitly requested for that task.
- Treat `main` as production-sensitive because Railway may deploy from it.
- Do not bypass existing risk limits, trading limits, approval checks, or safety controls unless the assigned task explicitly requires that change.

## Testing and verification

- Inspect the relevant code before editing.
- Make the smallest practical change that solves the assigned issue.
- Run existing tests and relevant checks before submitting a pull request.
- If the repository has no automated test for the changed behavior, document the manual verification performed.
- Do not claim a fix is complete if tests or verification fail.

## Documentation

- Summarize what changed and why in the pull request.
- Document any behavior, configuration, or operational changes that affect production.
- When work is associated with a GitHub issue, update or close the issue only when the requested work is actually complete.
- Preserve the repository's existing development/research notes when they are relevant to the task.

## Research snapshots

- Treat content under `research/` as reference material unless a task explicitly says otherwise.
- Do not import research snapshots into live trading code merely because they exist in the repository.
- When refreshing an upstream snapshot, record the upstream repository and pinned commit used.

## Default rule

When instructions are ambiguous, prefer a reversible branch/PR change over a direct production change.
