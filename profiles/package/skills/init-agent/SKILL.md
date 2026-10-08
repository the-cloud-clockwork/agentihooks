---
name: init-agent
description: >
  Open a Claude Code or Codex agent in a new herdr tab or terminal on WSL,
  macOS, or native Linux; Claude sessions are quota-routed across accounts. Accept a target directory, session name, opening
  prompt, and arbitrary Claude flags including --resume, --fork-session, and
  --model. Use when the operator says "init agent", "init-agent",
  "run claude terminal", "open a routed Claude session", or asks an agent to
  launch Claude in another terminal with a specific model or resumed session.
argument-hint: "[--dir PATH] [--name NAME] [--prompt-file PATH] [-- <claude flags>]"
---

# Init Agent

`agentihooks init-agent` owns host detection, terminal quoting, OAuth
account routing, and the start check. Never build a `herdr`, `wt.exe`,
`osascript`, or terminal command by hand.

The session opens in herdr when herdr is installed and enabled, otherwise in a
native terminal tab (Windows Terminal, macOS Terminal, a Linux emulator).
`AGENTIHOOKS_TERMINAL_HOST=herdr|native` or `--host` overrides that. When herdr
was chosen automatically and fails, the launch falls back to a native tab and
reports `herdr_error`.

## 1. Map the request

| Operator says | Launcher argument |
|---|---|
| a directory ("in ~/dev/foo", "at /repo") | `--dir <path>` — absolute, `~/…`, or relative to `$RUN_CLAUDE_BASE_DIR` → `~/dev` → `$HOME` |
| a session or tab name | `--name <name>` — default `s-YYMMDD-HHMMSS` |
| an opening prompt or task | `--prompt-file <file>` (see below) |
| a model ("use opus", "on fable") | `-- --model opus` or `-- --model fable` — the alias, never a versioned id; Claude resolves it to the latest release |
| resume a session | `-- --resume <session-id>` |
| fork a session | `-- --resume <session-id> --fork-session` |
| any other Claude flag | after `--`, unchanged |
| "next to me", "split" (caller runs inside herdr) | `--placement split` |
| "in its own workspace" | `--placement workspace` |
| a crew ("for crew alpha", "in the alpha workspace") | `--workspace <crew-label>` — every member gets a tab in that workspace |
| "in Windows Terminal", "not in herdr" | `--host native` |
| "open codex", "a codex agent" | `--agent codex` — agent flags after `--` go to `codex` |
| "open claude", "a claude agent" | `--agent claude` |

Rules:

- Without `--agent`, the launcher takes the harness of the account with a free
  session under its quota band and the fewest live sessions, and prints `agent=`
  and `agent_reason=` (`rotation`). `--handoff` always opens Claude on another account.
- Default herdr placement: a new tab in the caller's herdr workspace; outside
  herdr, a tab in the workspace named after the repository of `--dir`, created
  when missing.
- Launcher options (`--dir`, `--name`, `--prompt-file`, `--host`,
  `--placement`, `--workspace`, `--dry-run`, `--start-timeout`) go before `--`. Everything after `--` reaches Claude
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

agentihooks init-agent \
  --dir "<directory>" \
  --name "<name>" \
  --prompt-file "<scratch-dir>/opening-prompt.md" \
  -- <claude-flags>
```

Omit any option the request does not supply. Add `--dry-run` before `--` to
print the resolved launch without opening a terminal.

The command picks an `AH_CC_TOKEN_*` account inside the new terminal: each
account's live session cap comes from its five hour window (6 at 60% or more
left, 4 at 40 to 60, 3 at 10 to 40, 2 at 5 to 10, none below 5), an account under
5% of its week gets none, and the account with room and the fewest live sessions
takes it. `-- --route <slug>` forces one account and ignores the cap. Fable models include the separate Fable quota. With no configured or
eligible account, the terminal launches bare Claude with its existing direct,
keychain, or provider authentication.

## 3. Quota handoff

Run this only when a `QUOTA HANDOFF REQUIRED` directive says so; it gives the
document path, directory, and name. Use the handoff skill to write the Handoff v2
body and follow the runtime transfer command. The seat recap is derived from
Done, Stopped at and Next of that document. Submit only the handoff document:

```bash
agentihooks init-agent --handoff \
  --dir "<directory>" --name "<name>" --prompt-file "<handoff document>"
```

`--handoff` routes to an account other than this session's, never falls back to
bare Claude, waits for the new session's routing result, and marks this session
handed off.

| Output | Do |
|---|---|
| `handoff=done` (exit 0) | Tell the operator the `account` and `name` that took over, then stop. Tools stay blocked in this session, and its terminal (herdr pane or tab) closes at that stop unless `AGENTIHOOKS_HANDOFF_CLOSE_OLD=0`. |
| `handoff=failed` (exit 3) | Stop and report `route_error` to the operator. Do not retry on this account. |

## 4. Complete

The launcher waits three seconds after its pane or terminal opens before it
starts the agent. The command waits until the new terminal has started the launcher and the new
session has reported its route, then prints `key=value` lines: `status=started`,
`route_status` (`routed`, `bare`, `failed` or `pending`), and `account` when
routed. In herdr it also prints `workspace_id`, `tab_id`, `pane_id` and
`agent_name` (the name herdr's agent list shows). Exit zero is the completion
criterion; report `directory`, `name`, `host`, `account`, `pane_id` when present,
and `claude_args` from its output.

On a non-zero exit, report stderr verbatim. A start timeout discards the
launcher, so a late terminal cannot open a second session; relaunch at most
once, and only after the cause is fixed.
