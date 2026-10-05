---
name: run-in-terminal
description: >
  Run any command in any directory in a new herdr tab, or a native terminal tab
  (Windows Terminal from WSL2, Terminal.app, Linux).
  Defaults to $HOME with an interactive shell, but accepts an arbitrary
  command (claude, npm run dev, a chained `a && b`, anything) and an optional
  target directory. On a native-terminal failure it checks WSL interop and
  prints the exact command the operator must run. Use when the user says "run in terminal",
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
SKILL_DIR="$(dirname "$(readlink -f ~/.claude/skills/run-in-terminal/SKILL.md)" 2>/dev/null || echo "$HOME/.claude/skills/run-in-terminal")"
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

## Step 2: Run

<!-- DETERMINISTIC: agentihooks run-in-terminal picks the host and opens the tab -->
```bash
agentihooks run-in-terminal --dir "<directory>" --title "<title>" -- <command>
```

Omit `--dir` for `$HOME`, `--title` for the directory name, and the command
for an interactive shell. The tab opens in herdr when herdr is installed and
enabled (a tab in the caller's herdr workspace, else the directory's repository
workspace; `--workspace <label>` picks one), otherwise in a native terminal tab
through `scripts/02_run_terminal.sh`. `--host herdr|native` overrides.
A single argument after `--` runs as a raw shell string; several arguments are
shell-quoted, so each one, a quoted prompt included, arrives whole.

The output is `key=value` lines: `host`, `directory`, `title`, `command`, and in
herdr `workspace_id`, `tab_id`, `pane_id`. Report them.

## Step 3: Native failures

When it exits non-zero with `host=native`, run the interop check and show its
remediation output verbatim. Do NOT attempt `sudo` or modify system files — the
operator must do it. After they confirm, run Step 2 again.

```bash
"$SKILL_DIR"/scripts/01_check_interop.sh
```

---

## Extracted Scripts

| Script | Purpose | Idempotent | Validated |
|--------|---------|-----------|-----------|
| `01_check_interop.sh` | Verify WSL interop is enabled for wt.exe | Yes | Syntax-checked |
| `02_run_terminal.sh` | Open a Windows Terminal tab and run a command at a directory | No (new tab each call) | Syntax-checked |
