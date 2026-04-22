# Agenticore Worker

## Guidelines
- Commit with descriptive messages
- Focus on the task, be thorough, test your changes

## Retry Circuit Breaker (CRITICAL)
- If the same operation fails 5+ times in a row, DO NOT keep retrying
- Launch 2 `error-researcher` agents (subagent_type="error-researcher", model="haiku") in parallel to search the web
  - Agent 1: search for the exact error message
  - Agent 2: search for the tool/command + common causes
- Wait for results, then apply findings with a DIFFERENT approach
- If error-researcher agent is unavailable, use WebSearch directly
- The hook system enforces this — a hard block activates at 10 consecutive failures

## Security
- Never handle real credentials, API keys, tokens, or passwords in plaintext
- Reference secrets via environment variables only (e.g. `$MY_API_KEY`, not the value)
- If a task requires credentials, ask the user to configure them as env vars
- Never echo, log, print, or commit secret values
- If you encounter a credential value in context, treat it as an error and stop
