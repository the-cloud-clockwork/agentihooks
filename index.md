---
layout: home
title: Home
nav_order: 1
description: "AgentiHooks — Hook system and MCP tool server for Claude Code agents."
permalink: /
---

# AgentiHooks
{: .fs-9 .fw-700 }

Lifecycle hooks and 26 MCP tools for Claude Code — install once, work everywhere.
{: .fs-5 .text-grey-dk-100 .mb-6 }

<div class="hero-actions text-center mb-8" markdown="0">
  <a href="#install" class="btn btn-primary fs-5 mr-2">Get Started</a>
  <a href="https://github.com/The-Cloud-Clock-Work/agentihooks" class="btn fs-5" target="_blank">View on GitHub</a>
</div>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/blob/main/LICENSE)
[![CI](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agentihooks/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
{: .text-center .mb-8 }

---

## Install
{: #install }

```bash
pip install agentihooks
```

Then wire everything into Claude Code in one command:

```bash
agentihooks global
```

That's it. Hooks are active and 26 MCP tools are registered the next time you start `claude`.

---

## Choose a profile

Profiles set the agent's personality and tool permissions. The default profile works for most people.

```bash
# See what's available
agentihooks global --list-profiles

# Install with a specific profile
agentihooks global --profile coding

# Check which profile is active
agentihooks global --query
```

---

## Load your secrets — the `agentienv` shell function

Claude Code expands `${VAR}` in MCP configs from its own process environment. The cleanest way to get secrets into that environment is the `agentienv` shell function:

```bash
# One-time setup — writes a managed block to ~/.bashrc
agentihooks --loadenv

# Reload your shell
source ~/.bashrc
```

`agentienv` is now **auto-called** on every new shell — your vars load automatically. You only need to call it manually if you add new env files mid-session:

```bash
agentienv        # reload vars after adding a new *.env file
claude           # inherits all your vars
```

All `${VAR}` placeholders in MCP server configs resolve automatically.

---

## Restrict which tools load

By default all 26 tools across all 8 categories are active. Use environment variables in the MCP server's `env` block (inside `~/.claude.json`) to cut that down.

**Restrict by category** — only load the categories you need:

```json
"env": {
  "MCP_CATEGORIES": "aws,utilities"
}
```

Valid category names (comma-separated, any order):

```
aws  email  messaging  storage
database  compute  observability  utilities
```

**Restrict to specific tools** — allowlist exact tool names within the loaded categories:

```json
"env": {
  "MCP_CATEGORIES": "aws,utilities",
  "ALLOWED_TOOLS": "aws_get_profiles,aws_get_account_id,hooks_list_tools"
}
```

`ALLOWED_TOOLS` is an **allowlist** — only the tools you name will be active. Tools not in the list are removed at server startup.

**Where to edit:** open `~/.claude.json`, find the `hooks-utils` server under `mcpServers`, and update its `env` block. Restart Claude Code for the change to take effect.

**Verify what's active:** ask Claude Code to call `hooks_list_tools()` — it returns the exact set of loaded categories and tool names.

---

## Per-project MCP tools

Don't want a global install? Wire the MCP server into a single project instead:

```bash
agentihooks project ~/dev/my-project
agentihooks project ~/dev/my-project --profile coding
```

This writes a `.mcp.json` directly into the project directory.

---

## Add more MCP servers

Drop `.json` files with a `mcpServers` key into `~/.agentihooks/`, then use the interactive MCP manager:

```bash
# List available MCP files
agentihooks mcp

# Two-stage install: pick a file, then pick which servers to install
agentihooks mcp install

# Two-stage uninstall: pick a file, then pick which servers to remove
agentihooks mcp uninstall

# Install a specific file directly (all servers, no prompting)
agentihooks mcp add /path/to/.mcp.json

# Re-apply all installed files after edits
agentihooks mcp sync
```

Registered files are tracked in `~/.agentihooks/state.json` and re-applied automatically on every `agentihooks global` run.

---

## Fork & extend

AgentiHooks is a platform, not just a tool. Fork the repo and you immediately inherit:

- The full hook lifecycle (SessionStart → Stop) wired into Claude Code
- 26 MCP tools across 8 categories, ready to use or filter down
- Profile system — swap agent personality and permissions with one flag
- Install scripts, settings management, and credential loading

**Add your own tools in three steps:**

1. Create `hooks/mcp/mytools.py` with a `register(server)` function
2. Add `"mytools": "hooks.mcp.mytools"` to `_registry.py`
3. Run `agentihooks global` — your tools are live

**Add your own profile:**

Create a directory under `profiles/<name>/` with `profile.yml`, `.mcp.json`,
and `.claude/CLAUDE.md`. Run `agentihooks global --profile <name>`.

**Stay merge-friendly:**

Your additions live in new files and new directories. Existing files are
untouched. When you pull upstream changes the diff is clean.

Full extension guide → [Extending AgentiHooks]({{ site.baseurl }}/docs/extending/)

---

## Uninstall

```bash
agentihooks uninstall        # prompts for confirmation
agentihooks uninstall --yes  # scripting / no prompt
```

User data in `~/.agentihooks/` (logs, memory, state) is left in place. Remove it manually if you want a full reset:

```bash
rm -rf ~/.agentihooks
```

---

## What you get

| | |
|---|---|
| **Lifecycle hooks** | Auto-log transcripts, inject session context, save memory on stop |
| **26 MCP tools** | AWS, email, SQS, S3, DynamoDB, PostgreSQL, observability, and more |
| **Profiles** | Swap agent personality and permissions with one flag |
| **`agentienv` shell function** | Clean, shell-native secret loading — auto-called on every new shell, no wrapper scripts |

Full details in the [docs]({{ site.baseurl }}/docs/getting-started/).

---

## Related projects

| Project | Description |
|---------|-------------|
| [agenticore](https://github.com/The-Cloud-Clock-Work/agenticore) | Claude Code runner and orchestrator (uses agentihooks) |
| [agentibridge](https://github.com/The-Cloud-Clock-Work/agentibridge) | MCP server for session persistence and remote control |

---

<p align="center">
  Built by <a href="https://github.com/The-Cloud-Clock-Work">The Cloud Clock Work</a> &middot;
  <a href="https://github.com/The-Cloud-Clock-Work/agentihooks/blob/main/LICENSE">MIT License</a>
</p>
