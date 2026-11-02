#!/usr/bin/env bash
# Check if this OS's terminal-launch mechanism is available.
# Linux/WSL2: WSL interop (wt.exe reachable), detected through binfmt_misc with a Windows binary call as fallback.
# macOS: Terminal.app via osascript.
# Exits 0 if the launch path works, exits 1 with remediation instructions if not.
# Idempotent: read-only check.
set -euo pipefail

if [[ "$(uname -s)" == "Darwin" ]]; then
  if command -v osascript >/dev/null 2>&1; then
    exit 0
  fi
  cat >&2 <<'EOF'
osascript not found — cannot launch Terminal.app from this shell.
osascript ships with macOS by default; if it's missing, something in the
base system is broken. (This is a macOS host — there is no WSL/interop
config to fix here.)
EOF
  exit 1
fi

if [[ -f /proc/sys/fs/binfmt_misc/WSLInterop ]]; then
  exit 0
fi

if wt.exe --help >/dev/null 2>&1; then
  exit 0
fi

cat >&2 <<'EOF'
WSL interop is disabled — cannot launch Windows Terminal from WSL2.

Run this command to enable it for the current session:

  echo ':WSLInterop:M::MZ::/init:PF' | sudo tee /proc/sys/fs/binfmt_misc/register

To make it permanent, add to /etc/wsl.conf:

  [interop]
  enabled=true
  appendWindowsPath=true

Then restart WSL (from PowerShell: wsl --shutdown).
EOF
exit 1
