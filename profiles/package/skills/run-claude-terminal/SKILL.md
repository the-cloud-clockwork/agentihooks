---
name: run-claude-terminal
description: >
  Open a smart, quota-routed Claude Code session in a new terminal on WSL,
  macOS, or native Linux. Accept a target directory, session name, opening
  prompt, and arbitrary Claude flags including --resume, --fork-session, and
  --model. Use when the operator says "run claude terminal",
  "run-claude-terminal", "open a routed Claude session", or asks an agent to
  launch Claude in another terminal with a specific model or resumed session.
argument-hint: "[--dir PATH] [--name NAME] [--prompt-file PATH] [-- <claude flags>]"
---

# Run Claude Terminal

`agentihooks claude-terminal` owns host detection, terminal quoting, OAuth
account routing, and the start check. Never build a `wt.exe`, `osascript`, or
terminal command by hand.

## 1. Map the request

| Operator says | Launcher argument |
|---|---|
| a directory ("in ~/dev/foo", "at /repo") | `--dir <path>` — absolute, `~/…`, or relative to `$RUN_CLAUDE_BASE_DIR` → `~/dev` → `$HOME` |
| a session or tab name | `--name <name>` — default `s-YYMMDD-HHMMSS` |
| an opening prompt or task | `--prompt-file <file>` (see below) |
| a model ("use opus", "on fable") | `-- --model <name>` |
| resume a session | `-- --resume <session-id>` |
| fork a session | `-- --resume <session-id> --fork-session` |
| any other Claude flag | after `--`, unchanged |

Rules:

- Launcher options (`--dir`, `--name`, `--prompt-file`, `--dry-run`,
  `--start-timeout`) go before `--`. Everything after `--` reaches Claude
  verbatim; put nothing else there.
- Write every opening prompt to a file with a quoted heredoc and pass
  `--prompt-file`. The quoted heredoc keeps apostrophes, quotes, `$`, and
  newlines literal; an inline `--prompt` breaks on them.
- Do not guess a session ID. When the operator names a session without an ID,
  ask for it.

## 2. Launch

```bash
cat > "<scratch-dir>/opening-prompt.md" <<'PROMPT_EOF'
<opening prompt, verbatim>
PROMPT_EOF

agentihooks claude-terminal \
  --dir "<directory>" \
  --name "<name>" \
  --prompt-file "<scratch-dir>/opening-prompt.md" \
  -- <claude-flags>
```

Omit any option the request does not supply. Add `--dry-run` before `--` to
print the resolved launch without opening a terminal.

The command selects the healthiest `AH_CC_TOKEN_*` account inside the new
terminal, skipping any account that already runs
`AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT` (default 2) live sessions while another
routable account has room; when every account is full the least-loaded one takes
it (`placement=overflow`). `-- --route <slug>` forces one account and ignores the
cap. Fable models include the separate Fable quota. With no configured or
eligible account, the terminal launches bare Claude with its existing direct,
keychain, or provider authentication.

## 3. Quota handoff

Run this only when a `QUOTA HANDOFF REQUIRED` directive says so; it gives the
document path, directory, and name. Write the handoff document first, with
these sections: goal, done so far (commits, PRs, evidence), in progress, exact
next steps, repository / worktree / branch, open risks, the operator's standing
instructions. Then:

```bash
agentihooks claude-terminal --handoff \
  --dir "<directory>" --name "<name>" --prompt-file "<handoff document>"
```

`--handoff` routes to an account other than this session's, never falls back to
bare Claude, waits for the new session's routing result, and marks this session
handed off.

| Output | Do |
|---|---|
| `handoff=done` (exit 0) | Tell the operator the `account` and `name` that took over, then stop. Tools stay blocked in this session. |
| `handoff=failed` (exit 3) | Stop and report `route_error` to the operator. Do not retry on this account. |

## 4. Complete

The command waits until the new terminal has started the launcher and the new
session has reported its route, then prints `key=value` lines: `status=started`,
`route_status` (`routed`, `bare`, `failed` or `pending`), and `account` when
routed. Exit zero is the completion criterion; report `directory`, `name`,
`host`, `account`, and `claude_args` from its output.

On a non-zero exit, report stderr verbatim. A start timeout discards the
launcher, so a late terminal cannot open a second session; relaunch at most
once, and only after the cause is fixed.
