# Bundles

A bundle is a single external directory containing all your personal agentihooks customizations -- custom profiles, MCP configs, skills, agents, commands, and rules. Agentihooks is the engine; the bundle is your data.

## Quick Start

```bash
# Link your bundle and install (one command)
agentihooks init --bundle ~/dev/my-tools

# See everything available
agentihooks --list-profiles

# Use a bundle profile
agentihooks init --profile my-custom-profile

# Per-repo config
agentihooks init --repo ~/dev/some-project
```

## Bundle Layout

```
my-tools/                                   <- the bundle directory
├── .claude/                                # Bundle-global assets (layer 2 of 3-layer merge)
│   ├── .mcp.json                           # Bundle MCP servers
│   ├── skills/                             # Bundle-global skills
│   ├── agents/                             # Bundle-global agents
│   ├── commands/                           # Bundle-global commands
│   └── rules/                              # Bundle-global rules
└── profiles/
    ├── infra-ops/                           # Custom profile
    │   ├── CLAUDE.md                        # System prompt (at profile ROOT)
    │   ├── profile.yml                      # name, description, mcp_categories, claude launch config
    │   └── .claude/
    │       ├── settings.overrides.json      # Per-profile settings overrides
    │       ├── .mcp.json                    # Profile MCP servers
    │       ├── skills/                      # Profile-specific skills
    │       ├── agents/                      # Profile-specific agents
    │       ├── commands/                    # Profile-specific commands
    │       └── rules/                       # Profile-specific rules
    └── restricted/
        └── ...                              # Same structure
```

## How It Works

1. `agentihooks init --bundle <path>` stores the bundle path in `~/.agentihooks/state.json` and runs the global install
2. On subsequent `agentihooks init` runs, the linked bundle is automatically used
3. Profiles inside `profiles/` are **auto-discovered** by `--list-profiles`
4. `agentihooks init --profile <name>` checks built-in profiles first, then bundle

Only **one bundle** can be linked at a time.

## 3-Layer Merge

When `agentihooks init` runs, skills, agents, commands, rules, and MCP servers are merged from three layers:

| Layer | Source | Description |
|-------|--------|-------------|
| 1 (base) | agentihooks `.claude/` | Built-in assets from the agentihooks repo |
| 2 (bundle) | bundle `.claude/` | Bundle-global customizations |
| 3 (profile) | `profiles/<name>/.claude/` | Profile-specific overrides |

Later layers override earlier ones. This lets you start with the agentihooks base, add team customizations via the bundle, and fine-tune per profile.

For settings, the merge order is: `_base/settings.base.json` -> profile `.claude/settings.overrides.json` -> OTEL.

For MCP servers: hooks-utils + bundle `.claude/.mcp.json` + profile `.claude/.mcp.json`.

## Profile Resolution Order

When you run `agentihooks init --profile X`:

1. Check `profiles/X` in the agentihooks repo (built-in)
2. Check `profiles/X` in the linked bundle
3. Error if not found

Built-in profiles always take precedence. Name your bundle profiles to avoid conflicts.

## Per-Repo Config

After linking a bundle and setting a global profile, you can configure per repo:

```bash
agentihooks init --repo ~/dev/my-project
```

## Permission Tiers

Built-in profiles define escalating permission tiers:

| Profile | Mode | Deny | Ask |
|---------|------|------|-----|
| default | `default` | -- | git push, rm -rf, docker rm, kubectl delete |
| coding | `acceptEdits` | Protected branch pushes, merge, gh CLI | git push, rm -rf, docker, kubectl |
| admin | `auto` | -- | force push, rm -rf / |

Evaluation order: **deny > ask > allow** (first match wins).

## New Machine Setup

```bash
# 1. Install agentihooks
git clone https://github.com/The-Cloud-Clock-Work/agentihooks
cd agentihooks
uv venv ~/.agentihooks/.venv
uv pip install --python ~/.agentihooks/.venv/bin/python -e ".[all]"

# 2. Clone your tools repo and install with bundle
git clone https://github.com/you/my-tools ~/dev/my-tools
agentihooks init --bundle ~/dev/my-tools --profile default

# 3. Reload shell
source ~/.bashrc

# Done -- all profiles, skills, agents, commands, rules, and MCPs active
```
