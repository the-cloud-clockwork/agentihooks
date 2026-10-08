---
title: Bundles
parent: Getting Started
nav_order: 9
---
# Bundles

A bundle is a single external directory containing all your personal agentihooks customizations — custom profiles, MCP configs, skills, agents, commands, and rules. Agentihooks is the engine; the bundle is your data.

For purpose built swarms, use [Swarm Overlays](overlays.md) or the packaged
`swarm-maker` skill to add capabilities to the five base roles.

> **Public example**: [`agentihooks-bundle-example`](https://github.com/The-Cloud-Clockwork/agentihooks-bundle-example) — a minimal, working bundle you can clone, inspect, and fork as the starting point for your own.

## Quick Start

```bash
# No bundle yet: lay out an empty one, git init it and link it
agentihooks bundle new ~/dev/my-tools

# Add an overlay profile worn on a base role, then validate it
agentihooks overlay new backtest-tuner --wears engineer
agentihooks overlay check backtest-tuner

# Commit it: a rendered agent records the bundle HEAD at render time; uncommitted edits are not in it
git -C ~/dev/my-tools add README.md enforcements.json .claude profiles
git -C ~/dev/my-tools commit -m "Add backtest-tuner overlay"

# Link your bundle and install (one command)
agentihooks init --bundle ~/dev/my-tools

# See everything available
agentihooks --list-profiles

# Use a bundle profile
agentihooks init --profile my-custom-profile

# Chain multiple profiles (comma-separated)
agentihooks init --profile coding,my-custom-profile

# Update bundle from remote
agentihooks bundle pull
```

## Bundle Layout

```
my-tools/                                   <- the bundle directory
├── enforcements.json                       # Bundle-global enforcements (optional "matcher" per entry)
├── .claude/                                # Bundle-global assets (layer 2 of 3-layer merge)
│   ├── .mcp.json                           # Bundle MCP servers
│   ├── CLAUDE.md                           # OPTIONAL — shared directives prepended ahead of every profile
│   ├── skills/                             # Bundle-global skills
│   ├── agents/                             # Bundle-global agents
│   ├── commands/                           # Bundle-global commands
│   ├── rules/                              # Bundle-global rules
│   └── conditions/                         # Bundle-global conditions, run in place (docs/hooks/conditions.md)
└── profiles/
    ├── infra-ops/                           # Custom profile
    │   ├── CLAUDE.md                        # System prompt (at profile ROOT)
    │   ├── profile.yml                      # name, description, otel config, allowedOverlays, claude launch config; an overlay sets kind: overlay and wears: [base roles]
    │   ├── enforcements.json                # Profile enforcements
    │   └── .claude/
    │       ├── settings.overrides.json      # Per-profile settings overrides
    │       ├── .mcp.json                    # Profile MCP servers
    │       ├── skills/                      # Profile-specific skills
    │       ├── agents/                      # Profile-specific agents
    │       ├── commands/                    # Profile-specific commands
    │       ├── rules/                       # Profile-specific rules
    │       └── conditions/                  # Profile conditions; same filename overrides the bundle's
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

For MCP servers: agentihooks + bundle `.claude/.mcp.json` + profile `.claude/.mcp.json`.

## Shared `CLAUDE.md`

`~/.claude/CLAUDE.md` is assembled from up to three sources, in this order:

```
[ bundle .claude/CLAUDE.md ]  ->  [ profile CLAUDE.md (one per chained profile) ]  ->  [ all enabled bundle manifestos ]
```

An **optional** `<bundle>/.claude/CLAUDE.md` holds directives every profile should
share, so they are written once instead of duplicated into each profile. It is
prepended exactly once per install — including for a chained
`--profile a,b` — inside these markers:

```
<!-- BEGIN BUNDLE CLAUDE.md (auto-injected by agentihooks init) -->
...
<!-- END BUNDLE CLAUDE.md -->
```

Because profile content is written *after* the shared block, a profile can still
override shared guidance by restating it. Keep profile `CLAUDE.md` files to what
is genuinely profile-specific.

Re-running `agentihooks init` replaces the block in place — it never stacks. If the
bundle has no `.claude/CLAUDE.md`, or no profile in the chain has a `CLAUDE.md` to
prepend onto, the step is a no-op.

Every `*.md` file under the linked bundle's `manifestos/` directory is appended
in filename order; `README.md` is excluded. Set
`AGENTIHOOKS_SKIP_MANIFESTO=uno,dos,tres` to omit named manifestos. Names may
include or omit `.md`. `CI_MANIFESTO_PATH` remains a single-file compatibility
override, and `MANIFESTOS_DIR` can replace the bundle directory.

A manifesto may open with front matter naming the package roles that receive it:

```markdown
---
roles: [engineer, planner, qa, master]
---
# Development Manifesto
```

A rendered profile home receives a manifesto when the chain's package role
(`package:<role>`) is in its `roles`, or when the manifesto has no `roles` key, so
manifestos without front matter reach every role. A chain with no package role
receives every manifesto. The front matter never reaches rendered text. A bundle
profile overrides the roles by name in `profile.yml`; profiles apply in chain
order and a later one wins:

```yaml
manifestos:
  include: [CI-GUARDRAILS-MANIFESTO]
  exclude: [DEVELOPMENT-MANIFESTO]
```

`CI_MANIFESTO_ENABLED: "false"` in a chain profile's settings `env` renders no
manifestos for that chain. `agentihooks manifestos list [--bundle PATH]` prints
which package roles receive each manifesto.

## Profile Resolution Order

When you run `agentihooks init --profile X`:

1. Check `profiles/X` in the agentihooks repo (built-in)
2. Check `profiles/X` in the linked bundle
3. Error if not found

Built-in profiles always take precedence. Name your bundle profiles to avoid conflicts.

## Permission Tiers

Built-in profiles define escalating permission tiers:

| Profile | Mode | Deny | Ask |
|---------|------|------|-----|
| default | `auto` | Push to main/master, force push | *(empty)* |
| coding | `acceptEdits` | Protected branch pushes, merge, gh CLI | git push, rm -rf, docker, kubectl |
| admin | `bypassPermissions` | *(none)* | *(empty)* — all tools explicitly allowed |

Evaluation order: **deny > ask > allow** (first match wins).

## New Machine Setup

```bash
# 1. Install agentihooks
git clone https://github.com/The-Cloud-Clockwork/agentihooks
cd agentihooks
uv venv .venv
uv pip install --python .venv/bin/python -e ".[all]"

# 2. Clone your tools repo and install with bundle
git clone https://github.com/you/my-tools ~/dev/my-tools
agentihooks init --bundle ~/dev/my-tools --profile default

# 3. Reload shell
source ~/.bashrc

# Done -- all profiles, skills, agents, commands, rules, and MCPs active
```
