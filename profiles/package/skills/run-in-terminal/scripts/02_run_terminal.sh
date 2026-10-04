#!/usr/bin/env bash
set -euo pipefail
# Run a command in a new terminal tab/window, cd'd to a directory.
#
# Usage: 02_run_terminal.sh [directory] [tab-title] [command]
#   directory  target dir to cd into. Default: $HOME.
#   tab-title  Windows Terminal tab title (WSL2 path only — Terminal.app has
#              no scriptable tab-title API). Default: basename of directory.
#   command    raw shell command to run after cd. Default: interactive bash.
#
# The command is concatenated raw into `bash -lc` by design — chained
# commands, pipes, and quotes all pass through.
#
# Linux/WSL2: uses the wt.exe -> powershell.exe -> wsl.exe -> bash chain from
# agentihooks session_registry.py to avoid wt.exe semicolon parsing bugs.
# macOS: launches via `osascript -e 'tell application "Terminal" to do script'`.
#
# Requires this OS's terminal-launch mechanism (run 01_check_interop.sh first).
# Idempotent: opens a new tab/window each invocation (side-effect by design).

DIR="${1:-$HOME}"
TITLE="${2:-$(basename "$DIR")}"
CMD="${3:-}"

if [[ ! -d "$DIR" ]]; then
  echo "ERROR: directory does not exist: $DIR" >&2
  exit 1
fi

# Resolve to absolute path
DIR="$(cd "$DIR" && pwd)"

# Shell-quote for bash -lc embedding
shell_quote() { echo "'${1//\'/\'\\\'\'}'"; }

if [[ -n "$CMD" ]]; then
  INNER_BASH="cd $(shell_quote "$DIR") && $CMD"
else
  INNER_BASH="cd $(shell_quote "$DIR") && exec bash"
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  # AppleScript-quote for `do script "..."` embedding
  as_quote() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  osascript -e "tell application \"Terminal\" to do script \"$(as_quote "$INNER_BASH")\"" >/dev/null
  echo "Opened Terminal.app at $DIR"
  exit 0
fi

DISTRO="${WSL_DISTRO_NAME:-}"

# PowerShell-quote for -Command embedding
ps_quote() { echo "'${1//\'/\'\'}'"; }

WSL_CALL="wsl.exe"
if [[ -n "$DISTRO" ]]; then
  WSL_CALL+=" -d $(ps_quote "$DISTRO")"
fi
WSL_CALL+=" -- bash -lc $(ps_quote "$INNER_BASH")"

PS_CMD="& { $WSL_CALL }"

wt.exe -w 0 new-tab \
  --title "$TITLE" \
  powershell.exe -NoExit -NoProfile -Command "$PS_CMD" &

echo "Opened terminal tab '$TITLE' at $DIR"
