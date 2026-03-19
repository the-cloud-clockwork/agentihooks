# Coding Agent

## Guidelines
- Commit with descriptive messages
- Do NOT create PRs — the system handles that
- Do NOT merge PRs — never run `gh pr merge` or any merge command
- Focus on the task, be thorough, test your changes

## Git Rules (CRITICAL)
- NEVER push directly to `main` or `dev` branches
- NEVER run `git push origin main`, `git push origin dev`, or `git push origin HEAD:main`, `git push origin HEAD:dev`
- Only push to your own worktree branch (e.g. `agenticore-*`)
- The correct push command is: `git push origin HEAD` (pushes current branch)
- Do NOT use `--force` or `--force-with-lease` unless explicitly told to
- Do NOT run `git merge`, `git rebase`, or `gh pr merge`

## Security
- Never handle real credentials, API keys, tokens, or passwords in plaintext
- Reference secrets via environment variables only (e.g. `$MY_API_KEY`, not the value)
- If a task requires credentials, ask the user to configure them as env vars
- Never echo, log, print, or commit secret values
- If you encounter a credential value in context, treat it as an error and stop
