---
title: Per-Project Configuration
parent: Getting Started
nav_order: 5
---

# Per-Project Configuration
{: .no_toc }

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Overview

Each project can have its own profile, MCP whitelist, and system prompt — independent of the global install. Per-project configuration is driven by a `.agentihooks.json` file at the project root.

Running `agentihooks init --local` (or `agentihooks init --repo <path>`) generates two files inside `.claude/`:

| Generated file | Purpose | Gitignored? |
|---|---|---|
| `.claude/settings.local.json` | Env vars, permissions, MCP whitelist | Yes (auto) |
| `.claude/CLAUDE.local.md` | System prompt from the resolved profile | Yes (auto) |

Both are the highest-priority configuration files in Claude Code — they override global `~/.claude/settings.json` and `~/.claude/CLAUDE.md` for that project.

---

## `.agentihooks.json`

Create this file at your project root. It is meant to be **committed** to the repo so all collaborators share the same agent configuration.

```json
{
  "profile": "coding",
  "enabledMcpServers": [
    "gateway-publish",
    "gateway-core",
    "gateway-pm"
  ]
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `profile` | string | Profile name or comma-separated chain (e.g. `"coding,colt"`). Overrides the global profile. |
| `settings_profile` | string | Settings-only overlay profile. Applies `settings.overrides.json` and `.mcp.json` from the named profile without changing rules, CLAUDE.md, or skills. See [Settings Profiles](profiles.md#settings-profiles--independent-settings-layer). |
| `enabledMcpServers` | array | MCP servers to whitelist. All others are disabled by default. |
| `disabledMcpServers` | array | Additional servers to disable (for project-scope `.mcp.json` connectors). |
| `permissions.deny` | array | Extra tool patterns to deny (merged with profile rules). |
| `permissions.ask` | array | Extra tool patterns requiring confirmation. |
| `env` | object | Additional env vars merged into `settings.local.json`. |
| `otel` | object | OpenTelemetry overrides for this project. |

---

## Quick start

```bash
# Navigate to your project
cd ~/dev/my-project

# Create .agentihooks.json (or let init create one interactively)
cat > .agentihooks.json << 'EOF'
{
  "profile": "coding",
  "enabledMcpServers": ["gateway-core", "hooks-utils"]
}
EOF

# Generate settings.local.json + CLAUDE.local.md
agentihooks init --local
```

Output:

```
[OK] Read /home/user/dev/my-project/.agentihooks.json
  [OK] Wrote disabledMcpServers to ~/.claude.json (30 servers)
[OK] Wrote /home/user/dev/my-project/.claude/settings.local.json
     Profile: coding
     Enabled MCPs (2): ['gateway-core', 'hooks-utils']
     Blacklisted MCPs (30): [...]
  [OK] Wrote CLAUDE.local.md (coding)
```

---

## Profile override

The `profile` field controls which profile generates the project's local configuration:

- **`settings.local.json`** — env vars and permissions from the profile's `settings.overrides.json`
- **`CLAUDE.local.md`** — generated from the profile's `CLAUDE.md`
- **MCP whitelist** — the profile's `enabledMcpServers` from `profile.yml` are unioned with the repo's whitelist

### Profile chains

Comma-separated profiles are supported:

```json
{
  "profile": "coding,colt"
}
```

This produces:
- `CLAUDE.local.md` concatenated from both profiles (with `<!-- profile: name -->` markers and `---` separators)
- Settings overrides merged sequentially (coding first, colt on top)
- MCP whitelists unioned across both profiles

### Querying the active profile

```bash
agentihooks --query
```

When run from a directory with `.agentihooks.json`:
```
coding (local)
```

When run from a directory without one:
```
colt (global)
```

### One-shot profile override

Use `--profile` to override without changing `.agentihooks.json`:

```bash
agentihooks init --local --profile admin
```

This generates `CLAUDE.local.md` and `settings.local.json` using the `admin` profile, but `.agentihooks.json` retains its original `profile` value.

---

## CLAUDE.local.md

This file is generated from the resolved profile's `CLAUDE.md` and written to `.claude/CLAUDE.local.md`. Claude Code loads it as a project-level system prompt that overrides the global `~/.claude/CLAUDE.md`.

| Scenario | Behavior |
|---|---|
| Single profile (`"coding"`) | Content is copied directly from the profile's `CLAUDE.md` |
| Profile chain (`"coding,colt"`) | All profiles' `CLAUDE.md` files concatenated with markers |
| No `CLAUDE.md` in profile | File is not generated |

Chain mode output:

```markdown
<!-- profile: coding -->
# Coding Agent
...

---

<!-- profile: colt -->
# Colt Profile
...
```

{: .note }
`CLAUDE.local.md` is auto-gitignored. It is regenerated every time `agentihooks init --local` runs or the sync daemon detects source changes.

---

## MCP whitelist

The `enabledMcpServers` field specifies which MCP servers should be available in this project. All other servers registered in `~/.claude.json` are automatically disabled for the project.

```json
{
  "enabledMcpServers": ["gateway-publish", "gateway-core", "gateway-pm"]
}
```

The whitelist is written to the project's `disabledMcpServers` in `~/.claude.json` (inverse logic — everything not in the whitelist is added to the disabled list).

### Sources of whitelist

The effective whitelist for a project is the union of:

1. **Profile `enabledMcpServers`** — from `profile.yml` of the resolved profile (or chain)
2. **Repo `enabledMcpServers`** — from `.agentihooks.json`
3. **User-enabled MCPs** — servers manually toggled on in Claude Code's `/mcp` menu (tracked in `state.json`)

### Hierarchy-aware blacklist

When parent and child projects both exist in `~/.claude.json`, the system automatically prevents the parent from blocking servers that child projects need.

Example:

```
/agentihub/                               → parent project (no .agentihooks.json)
/agentihub/agents/publishing/package/     → child project with enabledMcpServers
```

The parent's `disabledMcpServers` will automatically exclude `gateway-publish`, `gateway-core`, and `gateway-pm` because the child whitelists them. This is necessary because Claude Code resolves project settings by walking up the directory tree — without this, the parent's blanket blacklist would override the child's whitelist.

{: .important }
This hierarchy awareness applies to `agentihooks init` (global), `agentihooks init --local`, and the sync daemon. All three code paths respect child project whitelists.

---

## Orphan pruning

When MCP servers are removed from a bundle or profile `.mcp.json`, they can become orphaned in `~/.claude.json` — still registered but no longer defined anywhere. The sync daemon automatically detects and removes these orphaned entries on every poll cycle.

The pruning compares the live `mcpServers` in `~/.claude.json` against the authoritative set from all source files (bundle, profile, hooks-utils, state-tracked `.mcp.json` files). Any server not in the authoritative set is removed.

{: .note }
Servers prefixed with `claude.ai ` (web-session managed) are never pruned — they are managed by Claude's web interface, not agentihooks.

---

## Sync daemon behavior

The sync daemon (`agentihooks daemon`) monitors source files and automatically re-applies per-project settings when changes are detected. For registered projects (added via `init --repo` or `init --local`), the daemon:

1. Re-generates `settings.local.json` and `CLAUDE.local.md` when profile files change
2. Adds new MCP servers to disabled lists (respecting whitelists)
3. Prunes orphaned MCP servers
4. Backfills `disabledMcpServers` for newly discovered project entries

The daemon is always restarted on `agentihooks init` to ensure it runs the latest code.

---

## Example: monorepo with multiple agents

```
my-monorepo/
├── .agentihooks.json           → {"profile": "colt"}
├── agents/
│   ├── publisher/
│   │   └── .agentihooks.json   → {"profile": "coding", "enabledMcpServers": ["gateway-publish"]}
│   └── reviewer/
│       └── .agentihooks.json   → {"profile": "coding", "enabledMcpServers": ["gateway-core"]}
└── infra/
    └── .agentihooks.json       → {"profile": "admin", "enabledMcpServers": ["gateway-infra"]}
```

Each subdirectory gets its own profile and MCP access. The root project automatically allows `gateway-publish`, `gateway-core`, and `gateway-infra` through (hierarchy-aware blacklist).

```bash
# Initialize all projects
agentihooks init                             # global install
cd my-monorepo && agentihooks init --local   # root project
cd agents/publisher && agentihooks init --local
cd agents/reviewer && agentihooks init --local
cd infra && agentihooks init --local
```
