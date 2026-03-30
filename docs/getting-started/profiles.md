---
title: Profiles
nav_order: 3
---

# Profiles
{: .no_toc }

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## What is a profile?

A **profile** is a named configuration bundle that controls:

- Which **agent system prompt** (`CLAUDE.md`) Claude Code loads
- Which **settings overrides** are applied (permissions, env vars)
- Which **MCP tool categories** are enabled (via `MCP_CATEGORIES`)
- Which **skills, agents, commands, and rules** are symlinked
- Model selection, turn limits, permission mode, and timeout settings (via `agentihooks claude`)

Profiles are stored under `profiles/<name>/` in the repository or in a linked bundle.

---

## Profile structure

```
profiles/
├── _base/
│   └── settings.base.json          # Canonical settings (hooks, permissions, MCP servers)
├── default/
│   ├── CLAUDE.md                    # Agent system prompt (at profile ROOT)
│   ├── profile.yml                  # agentihooks metadata + claude launch config
│   └── .claude/
│       ├── settings.overrides.json  # Per-profile settings overrides
│       ├── .mcp.json                # Profile MCP servers
│       ├── skills/                  # Profile-specific skills
│       ├── agents/                  # Profile-specific agents
│       ├── commands/                # Profile-specific commands
│       └── rules/                   # Profile-specific rules
└── coding/
    └── ...                          # Same structure
```

### `_base/settings.base.json`

This is the **single source of truth** for all settings. It contains:

- Hook event wiring (`hooks` -> shell commands)
- Tool permission allowances
- MCP server definitions

All paths use `/app` as a placeholder. The install script substitutes `/app` with the real repo path at render time.

### `CLAUDE.md` (at profile root)

The agent's system prompt. This file lives at the **profile root** (not inside `.claude/`). The install script symlinks `~/.claude/CLAUDE.md` to the chosen profile's `CLAUDE.md`.

### `.claude/settings.overrides.json`

Per-profile settings overrides that are merged on top of `_base/settings.base.json` during install. Lives inside the `.claude/` subdirectory.

### `profile.yml`

Contains fields for both agentihooks and the `agentihooks claude` launcher:

**agentihooks fields** (read by `install.py`):

- `name` -- profile identifier
- `description` -- shown by `--list-profiles`
- `mcp_categories` -- comma-separated tool categories to enable (default: `all`)

**claude launch fields** (read by `agentihooks claude`):

- `claude.model`, `claude.max_turns`, `claude.timeout` -- passed as Claude Code CLI args
- `claude.permission_mode` -- e.g. `bypassPermissions` maps to `--dangerously-skip-permissions`

```yaml
# agentihooks fields
name: coding
description: "Autonomous coding agent"
mcp_categories: aws,utilities,observability

# claude launch config
claude:
  model: sonnet
  max_turns: 80
  permission_mode: bypassPermissions
```

---

## 3-layer merge

When `agentihooks init` runs, skills, agents, commands, rules, and MCP servers are merged from three layers:

1. **agentihooks built-in** -- `.claude/` in the agentihooks repo
2. **Bundle global** -- `.claude/` in the linked bundle root
3. **Profile-specific** -- `profiles/<name>/.claude/`

Later layers override earlier ones. This lets you start with a shared base, add team customizations via the bundle, and fine-tune per profile.

---

## Listing profiles

```bash
agentihooks --list-profiles
```

Example output:

```
Available profiles:
  default
  coding
  admin
```

Profiles from both the agentihooks repo and linked bundle are listed.

---

## Switching profiles

Re-run init with `--profile`:

```bash
agentihooks init --profile coding
```

Or set the `AGENTIHOOKS_PROFILE` environment variable so you don't have to pass `--profile` every time:

```bash
export AGENTIHOOKS_PROFILE=coding
agentihooks init
```

This is especially useful in CI/Docker automation where the profile is set once in the container environment.

Either way, the command atomically:
1. Replaces the `~/.claude/CLAUDE.md` symlink
2. Updates `MCP_CATEGORIES` in the hook environment
3. Re-merges settings overrides
4. Re-symlinks skills, agents, commands, and rules (3-layer merge)
5. Re-merges MCP servers

The switch takes effect on the next Claude Code session.

---

## Querying the active profile

```bash
agentihooks --query
```

---

## Launching Claude with profile flags

The `agentihooks claude` command (alias: `agenti`) reads the `claude:` section from the active profile's `profile.yml` and maps it to Claude Code CLI flags:

```bash
agentihooks claude           # launch with profile settings
agenti                       # same thing (alias installed by init)
```

For example, `permission_mode: bypassPermissions` in profile.yml translates to `--dangerously-skip-permissions`.

---

## Creating a custom profile

1. Copy an existing profile:
   ```bash
   cp -r profiles/default profiles/myprofile
   ```

2. Edit `profiles/myprofile/profile.yml` to set model, turns, and categories.

3. Edit `profiles/myprofile/CLAUDE.md` with your custom system prompt.

4. Optionally add profile-specific assets in `profiles/myprofile/.claude/` (skills, agents, commands, rules, `.mcp.json`, `settings.overrides.json`).

5. Install the new profile:
   ```bash
   agentihooks init --profile myprofile
   ```

{: .note }
Profiles affect the **agent's persona, tool access, and asset selection** but not the underlying hook behavior. Hooks are always wired from `_base/settings.base.json` regardless of profile.
