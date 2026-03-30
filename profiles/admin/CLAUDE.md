# Admin Agent

## Guidelines
- Commit with descriptive messages
- Do NOT create PRs — the system handles that
- Focus on the task, be thorough

## Retry Circuit Breaker (CRITICAL)
- If the same operation fails 5+ times in a row, DO NOT keep retrying
- Launch 2 `error-researcher` agents (subagent_type="error-researcher", model="haiku") in parallel to search the web
  - Agent 1: search for the exact error message
  - Agent 2: search for the tool/command + common causes
- Wait for results, then apply findings with a DIFFERENT approach
- If error-researcher agent is unavailable, use WebSearch directly
- The hook system enforces this — a hard block activates at 10 consecutive failures

## Security
- Secrets scanning is in **warn-only** mode — detections are reported but never block operations
- You bear full responsibility for handling credentials safely
- Reference secrets via environment variables when possible (e.g. `$MY_API_KEY`, not the value)
- Never echo, log, print, or commit secret values unless explicitly instructed
- If you encounter a credential value in context, flag it to the user
