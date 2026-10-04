---
name: run-in-terminal
description: >
  Run any command in any directory in a new Windows Terminal tab from WSL2.
  Defaults to $HOME with an interactive shell, but accepts an arbitrary
  command (claude, npm run dev, a chained `a && b`, anything) and an optional
  target directory. Checks WSL interop first; if disabled, prints the exact
  command the operator must run. Use when the user says "run in terminal",
  "run-in-terminal", "run terminal", "run <cmd> in <dir>", "run this in <dir>", "run command in
  directory", "open claude at <path>", "open a shell at <path>", or
  "launch terminal at <path>".
argument-hint: "[command] [--dir <directory>] [--title <tab-title>]"
---

# Run In Terminal — Run Any Command in Any Directory

Opens a new terminal tab/window targeting any directory and runs an
arbitrary command there. On WSL2, opens a Windows Terminal tab via the
`wt.exe → powershell.exe → wsl.exe → bash` chain (same pattern as agentihooks
session_registry.py) to avoid wt.exe semicolon parsing bugs. On macOS, opens
a Terminal.app window via `osascript`.

The command is passed **raw** into `bash -lc`, so anything you can type at a
shell works: a single binary (`claude`), a chained command (`git pull && make`),
pipes, quotes, env assignments. With no command, the tab drops into an
interactive shell. With no directory, it lands in `$HOME`.

## Script Resolution

```bash
SKILL_DIR="$(dirname "$(readlink -f ~/.claude/skills/run-in-terminal/SKILL.md)" 2>/dev/null || echo "$HOME/.claude/skills/run-terminal")"
```

---

## Step 1: Parse Arguments

The **command** is the primary input; the directory is optional.

- **command** — the thing to run after `cd`'ing into the directory. Raw shell
  string. Default: none → drop into an interactive `bash`.
- **directory** (`--dir`, or "in <path>" / "at <path>") — where to run.
  Default: `$HOME`.
- **title** (`--title`) — tab title. Default: basename of the directory.

Natural-language → invocation mapping:

| Operator says | directory | command |
|---|---|---|
| "run terminal" / "open a shell" | `$HOME` | (interactive bash) |
| "run terminal at ~/dev/foo" | `~/dev/foo` | (interactive bash) |
| "run `npm test` in ~/dev/foo" | `~/dev/foo` | `npm test` |
| "open claude at /path" / "run claude in /path" | `/path` | `claude` |
| a claude session **with an opening prompt or a name** | — | use `/init-agent` — its prompt goes through a file; a prompt on this skill's raw command line breaks on the first apostrophe |
| "run `git pull && make` in /repo" | `/repo` | `git pull && make` |
| "run `claude --resume <id>` in /repo" | `/repo` | `claude --resume <id>` |

(`claude --resume <id>` is just another command — this skill does not perform
any handoff or session lookup; pass a fully-formed command.)

---

## Step 2: Check WSL Interop

<!-- DETERMINISTIC: verify wt.exe can execute from WSL2 -->
```bash
"$SKILL_DIR"/scripts/01_check_interop.sh
```

If this exits non-zero, **STOP**. Show the user the remediation output verbatim.
Do NOT attempt to run `sudo` or modify system files — the operator must do it.
After they confirm it's done, re-run this check.

---

## Step 3: Run the Terminal

<!-- DETERMINISTIC: launch wt.exe new-tab via the PowerShell bridge -->
```bash
"$SKILL_DIR"/scripts/02_run_terminal.sh "<directory>" "<title>" "<command>"
```

Positional args: arg1 = resolved directory (default `$HOME`), arg2 = tab title
(default basename of the directory), arg3 = command (optional; omit for an
interactive shell). To pass a command while keeping the default directory,
pass `"$HOME"` explicitly as arg1.

Report the result to the user: which directory, tab title, and command were used.

---

## Extracted Scripts

| Script | Purpose | Idempotent | Validated |
|--------|---------|-----------|-----------|
| `01_check_interop.sh` | Verify WSL interop is enabled for wt.exe | Yes | Syntax-checked |
| `02_run_terminal.sh` | Open a Windows Terminal tab and run a command at a directory | No (new tab each call) | Syntax-checked |
