# Bundles

A bundle is a single external directory containing all your personal agentihooks customizations — custom profiles, connectors, and MCP configs. Agentihooks is the engine; the bundle is your data.

## Quick Start

```bash
# Link your bundle (one time)
agentihooks bundle link ~/dev/my-tools/.agentihooks

# See everything available
agentihooks --list-profiles

# Use a bundle profile
agentihooks global --profile my-custom-profile

# Per-repo override
cd ~/dev/some-project
agentihooks init --profile coding
```

## Bundle Layout

```
my-tools/.agentihooks/              ← the bundle directory
  profiles/
    infra-ops/                       # custom profile
      profile.yml                    # name, description, mcp_categories
      settings.overrides.json        # permissions, env vars
      .claude/CLAUDE.md              # system prompt
    restricted/
      ...
  connectors/
    my-mcp-filter/                   # MCP tool deny rules
      connector.yml
      profiles/
        default/permissions.json
        coding/permissions.json
```

## How It Works

1. `agentihooks bundle link <path>` stores the path in `~/.agentihooks/state.json`
2. Connectors inside `connectors/` are **auto-linked** — no separate `connector link` needed
3. Profiles inside `profiles/` are **auto-discovered** by `--list-profiles` and `global --profile`
4. `agentihooks global --profile <name>` checks built-in profiles first, then bundle

Only **one bundle** can be linked at a time.

## CLI

```bash
agentihooks bundle link <path>    # Link a bundle directory
agentihooks bundle list           # Show bundle contents (profiles + connectors)
agentihooks bundle unlink         # Remove the bundle (auto-unlinks bundle connectors)
```

## Profile Resolution Order

When you run `agentihooks global --profile X`:

1. Check `profiles/X` in the agentihooks repo (built-in)
2. Check `profiles/X` in the linked bundle
3. Error if not found

Built-in profiles always take precedence. Name your bundle profiles to avoid conflicts.

## Per-Repo Config

After linking a bundle and setting a global profile, you can override per repo with `.agentihooks.json`:

```json
{
  "profile": "coding",
  "disabledMcpServers": ["gateway-media"],
  "permissions": {
    "deny": ["Bash(terraform apply *)"],
    "ask": ["Bash(terraform plan *)"]
  },
  "env": {
    "CUSTOM_VAR": "value"
  }
```

Apply with `agentihooks init` — writes `.claude/settings.local.json` (highest priority settings file in Claude Code). The profile's permissions + connector rules + repo overrides are all merged.

## Permission Tiers

Built-in profiles define escalating permission tiers:

| Profile | Mode | Deny | Ask |
|---------|------|------|-----|
| default | `default` | — | git push, rm -rf, docker rm, kubectl delete |
| coding | `acceptEdits` | Protected branch pushes, merge, gh CLI | git push, rm -rf, docker, kubectl |
| admin | `auto` | — | force push, rm -rf / |

Evaluation order: **deny > ask > allow** (first match wins).

Connectors add MCP-specific deny rules on top. Per-repo config adds more. Rules only stack — you can't relax restrictions, only add them. To relax, use a less restrictive profile.

## New Machine Setup

```bash
# 1. Install agentihooks
git clone https://github.com/The-Cloud-Clock-Work/agentihooks
cd agentihooks
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"

# 2. Install globally
agentihooks global

# 3. Clone your tools repo and link the bundle
git clone https://github.com/you/my-tools ~/dev/my-tools
agentihooks bundle link ~/dev/my-tools/.agentihooks

# 4. Reinstall with bundle
agentihooks global --profile default

# Done — all profiles, connectors, and MCP rules active
```
