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

Running `agentihooks init --local` (or `agentihooks init --repo <path>`) generates one file inside `.claude/`:

| Generated file | Purpose | Gitignored? |
|---|---|---|
| `.claude/settings.local.json` | Env vars, permissions, MCP whitelist | Yes (auto) |

It is the highest-priority project-level configuration file in Claude Code — it overrides global `~/.claude/settings.json` for that project. The system prompt rendered from the profile chain lives in the global `~/.claude/CLAUDE.md` only — per-project `.claude/CLAUDE.local.md` is no longer generated.

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
| `profile` | string | Profile name or comma-separated chain (e.g. `"coding,anton"`). Overrides the global profile. |
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

# Generate settings.local.json
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
```

---

## Profile override

The `profile` field controls which profile generates the project's local configuration:

- **`settings.local.json`** — env vars and permissions from the profile's `settings.overrides.json`
- **MCP whitelist** — the profile's `enabledMcpServers` from `profile.yml` are unioned with the repo's whitelist
- **System prompt** — rendered from the profile chain into the global `~/.claude/CLAUDE.md` (per-project `.claude/CLAUDE.local.md` is no longer generated)

### Profile chains

Comma-separated profiles are supported:

```json
{
  "profile": "coding,anton"
}
```

This produces:
- Global `~/.claude/CLAUDE.md` concatenated from both profile `CLAUDE.md` files (with `<!-- profile: name -->` markers and `---` separators)
- Settings overrides merged sequentially (coding first, anton on top)
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
anton (global)
```

### One-shot profile override

Use `--profile` to override without changing `.agentihooks.json`:

```bash
agentihooks init --local --profile admin
```

This generates `settings.local.json` using the `admin` profile, but `.agentihooks.json` retains its original `profile` value.

---

## System prompt

The profile chain's `CLAUDE.md` files are concatenated and written to the global
`~/.claude/CLAUDE.md`. Claude Code loads that as the system prompt for every
session. There is no per-project `.claude/CLAUDE.local.md` — that artifact was
removed because it was a fleet-wide duplicate of the global file. If a project
genuinely needs project-scoped instructions, hand-write them in the project's
top-level `CLAUDE.md` (Claude Code reads that automatically).

Chain mode output (in `~/.claude/CLAUDE.md`):

```markdown
<!-- profile: coding -->
# Coding Agent
...

---

<!-- profile: anton -->
# Anton Profile
...
```

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
This hierarchy awareness applies to `agentihooks init` (global) and `agentihooks init --local`. Both code paths respect child project whitelists.

---

## Orphan pruning

When MCP servers are removed from a bundle or profile `.mcp.json`, they can become orphaned in `~/.claude.json` — still registered but no longer defined anywhere. Run `agentihooks prune` to remove them; `agentihooks init` also runs the same pruning step.

The pruning compares the live `mcpServers` in `~/.claude.json` against the authoritative set from all source files (bundle, profile, hooks-utils, state-tracked `.mcp.json` files). Any server not in the authoritative set is removed.

{: .note }
Servers prefixed with `claude.ai ` (web-session managed) are never pruned — they are managed by Claude's web interface, not agentihooks.

---

## Example: monorepo with multiple agents

```
my-monorepo/
├── .agentihooks.json           → {"profile": "anton"}
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
