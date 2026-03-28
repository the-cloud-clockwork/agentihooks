# Connectors

Connectors are external adapters that extend agentihooks profiles with additional permissions, deny rules, and environment variables. They live **outside** the agentihooks repo — in your project repos, a shared tools repo, or anywhere on disk.

## Why Connectors?

When you connect MCP servers with hundreds of tools (e.g., a LiteLLM gateway with 400+ tools), every tool schema loads into Claude Code's context window. `permissions.deny` regex patterns (Claude Code v2.1.78+) remove denied tools from model context entirely — not just block calls, but strip them from the token budget.

Connectors let you define which tools to deny **per profile**, decoupled from agentihooks internals.

## Directory Layout

```
my-connector/
  connector.yml              # Required — metadata + base config
  profiles/
    default/
      permissions.json       # Deny/allow rules for the "default" profile
      env.json               # Optional — env var overrides
    coding/
      permissions.json
    admin/
      permissions.json
```

Profile directory names must match agentihooks profile names (`default`, `coding`, `admin`, etc.). Profiles without a matching directory are silently skipped.

## connector.yml

```yaml
name: my-connector
description: "What this connector does"
version: "1.0.0"

base:
  env:
    SOME_VAR: "value"           # Applied to ALL profiles
```

The `base.env` block is merged into `settings.json` regardless of which profile is active.

## permissions.json

```json
{
  "deny": [
    "mcp__gateway-prod__mediagen-.*",
    "mcp__gateway-prod__router-.*"
  ]
}
```

Patterns are regex, matching against Claude Code tool names. Format: `mcp__<server-entry>__<mcp-server>-<tool-name>`.

## env.json (optional)

```json
{
  "MY_PROFILE_VAR": "value"
}
```

Per-profile env var overrides. Merged into `settings.json` `env` block.

## CLI Commands

```bash
# Link a connector (registers path in ~/.agentihooks/state.json)
agentihooks connector link /path/to/my-connector

# List linked connectors
agentihooks connector list

# Preview what a connector would merge
agentihooks connector inspect /path/to/my-connector

# Remove a connector
agentihooks connector unlink my-connector
```

## How It Works

1. You run `agentihooks connector link /path/to/connector`
2. Agentihooks saves the **path** in `~/.agentihooks/state.json` — no files are copied
3. When you run `agentihooks global --profile default`:
   - Base settings loaded
   - Profile overrides applied
   - **Connectors merged** — for each linked connector with a `profiles/default/` directory:
     - `connector.yml → base → env` merged into env block
     - `profiles/default/permissions.json → deny` appended to permissions.deny
     - `profiles/default/env.json` merged into env block (if exists)
   - Result written to `~/.claude/settings.json`

Connectors are **additive only** — they append deny rules and merge env vars. They cannot remove existing agentihooks settings.

## Multiple Connectors

You can link multiple connectors. Their rules stack:

```bash
agentihooks connector link ~/dev/cc-colt-tools/connectors/antoncore-mcp
agentihooks connector link ~/dev/other-project/connectors/other-mcp
agentihooks connector link ~/dev/my-global-rules/
```

All deny rules from all linked connectors are appended together.

## Where to Put Connectors

Connectors are just directories — put them wherever makes sense:

| Location | Use Case |
|----------|----------|
| `project-repo/connectors/name/` | Project-specific tool filtering |
| `~/dev/cc-colt-tools/connectors/name/` | Shared personal tools repo |
| `~/my-global-connector/` | Standalone global rules |

## Example: MCP Tool Filtering

A connector that blocks publishing and media tools in the default profile, keeps only github/sonar/agenticore in coding:

**connector.yml:**
```yaml
name: my-gateway-filter
description: "Filter LiteLLM gateway tools by profile"
version: "1.0.0"

base:
  env:
    ENABLE_CLAUDEAI_MCP_SERVERS: "false"
```

**profiles/default/permissions.json:**
```json
{
  "deny": [
    "mcp__gateway-prod__mediagen-.*",
    "mcp__gateway-prod__paper2slides-.*",
    "mcp__gateway-prod__router-.*"
  ]
}
```

**profiles/coding/permissions.json:**
```json
{
  "deny": [
    "mcp__gateway-prod__mediagen-.*",
    "mcp__gateway-prod__paper2slides-.*",
    "mcp__gateway-prod__router-.*",
    "mcp__gateway-prod__atlassian-.*",
    "mcp__gateway-prod__grafana-.*",
    "mcp__gateway-prod__litellm_tools-.*"
  ]
}
```

Link it, reinstall, done:
```bash
agentihooks connector link /path/to/my-gateway-filter
agentihooks global --profile default
```
