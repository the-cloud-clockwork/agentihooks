---
name: run-claude-terminal
description: >
  Open a smart, quota-routed Claude Code session in a new terminal on WSL,
  macOS, or native Linux. Accept a target directory, session name, opening
  prompt, and arbitrary Claude flags including --resume, --fork-session, and
  --model. Use when the operator says "run claude terminal",
  "run-claude-terminal", "open a routed Claude session", or asks an agent to
  launch Claude in another terminal with a specific model or resumed session.
argument-hint: "[--dir PATH] [--name NAME] [--prompt TEXT] [-- <claude flags>]"
---

# Run Claude Terminal

Delegate the launch to Agentihooks so host detection, terminal quoting, and
OAuth account routing have one implementation.

## 1. Map the request

- Directory → `--dir <path>`. Omit it to use `$RUN_CLAUDE_BASE_DIR`, then
  `~/dev`, then `$HOME`.
- Session name → `--name <name>`. Omit it for `s-YYMMDD-HHMMSS`.
- Opening text → `--prompt <text>` or `--prompt-file <path>`.
- Put every Claude argument after `--`. Map “resume” to `--resume <id>`,
  “fork” to `--fork-session`, and model choice to `--model <name>`.
- Pass any other Claude flag unchanged after `--`.

## 2. Launch

```bash
agentihooks claude-terminal \
  [--dir "<directory>"] \
  [--name "<name>"] \
  [--prompt "<opening-prompt>"] \
  -- <claude-flags>
```

The command selects the healthiest `AH_CC_TOKEN_*` account inside the new
terminal. Fable models automatically include the separate Fable quota.

For a non-launching check, add `--dry-run` before `--`.

## 3. Complete

Report the resolved directory, session name, host launcher, and Claude flags.
Success requires the launcher command to exit zero; otherwise report its error
verbatim.
