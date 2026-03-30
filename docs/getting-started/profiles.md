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
- Which **MCP tool categories** are enabled (via `MCP_CATEGORIES`)
- Model selection, turn limits, and timeout settings

Profiles are stored under `profiles/<name>/` in the repository.

---

## Profile structure

```
profiles/
├── _base/
│   └── settings.base.json      # Canonical settings (hooks, permissions, MCP servers)
├── default/
│   ├── profile.yml             # agentihooks: name, description, mcp_categories
│   │                           # agenticore: model, turns, timeout, etc.
│   ├── settings.overrides.json # Optional per-profile env/setting overrides
│   └── .claude/
│       └── CLAUDE.md           # Agent system prompt for this profile
└── coding/
    └── ...                     # Same structure
```

### `_base/settings.base.json`

This is the **single source of truth** for all settings. It contains:

- Hook event wiring (`hooks` → shell commands)
- Tool permission allowances
- MCP server definitions

All paths use `/app` as a placeholder. The install script substitutes `/app` with the real repo path at render time.

### `profile.yml`

Contains fields for both agentihooks and agenticore:

**agentihooks fields** (read by `install.py`):
- `name` — profile identifier
- `description` — shown by `--list-profiles`
- `mcp_categories` — comma-separated tool categories to enable (default: `all`)

**agenticore fields** (read by agenticore, ignored by agentihooks):
- `claude.model`, `claude.max_turns`, `claude.timeout`, etc. — passed as Claude Code CLI args

```yaml
# agentihooks fields
name: coding
description: "Autonomous coding agent"
mcp_categories: aws,utilities,observability

# agenticore fields
claude:
  model: sonnet
  max_turns: 80
```

### `.claude/CLAUDE.md`

The agent's system prompt. This is what Claude Code loads as its operating instructions. The install script symlinks `~/.claude/CLAUDE.md` to the chosen profile's `CLAUDE.md`.

---

## Listing profiles

```bash
agentihooks init --list-profiles
```

Example output:

```
Available profiles:
  default
  coding
```

---

## Switching profiles

Re-run the global install with `--profile`:

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
3. Re-merges the profile `.mcp.json`

The switch takes effect on the next Claude Code session.

---

## Querying the active profile

```bash
agentihooks init --query
```

---

## Creating a custom profile

1. Copy an existing profile:
   ```bash
   cp -r profiles/default profiles/myprofile
   ```

2. Edit `profiles/myprofile/profile.yml` to set model, turns, and categories.

3. Edit `profiles/myprofile/CLAUDE.md` with your custom system prompt.

4. Install the new profile:
   ```bash
   agentihooks init --profile myprofile
   ```

{: .note }
Profiles affect the **agent's persona and tool access** but not the underlying hook behavior. Hooks are always wired from `_base/settings.base.json` regardless of profile.

