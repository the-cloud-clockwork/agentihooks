---
title: Connectors
parent: Hook System
nav_order: 9
---
# Connectors

{: .warning }
The `agentihooks connector` CLI has been removed. Connector functionality is now handled through the **bundle system** and **profile-level settings overrides**. See [Bundles](bundles.md) for the current approach.

## Migration

If you were using standalone connectors, migrate their configurations into your bundle:

- **Permission deny rules** -- move into profile `.claude/settings.overrides.json` under the `permissions.deny` key
- **Environment variables** -- move into `~/.agentihooks/.env` or profile-level env overrides
- **Per-profile variations** -- create separate profiles in your bundle with different `settings.overrides.json` files

## How permissions work now

Permission deny rules are configured in the profile's `.claude/settings.overrides.json`:

```json
{
  "permissions": {
    "deny": [
      "mcp__gateway-prod__mediagen-*",
      "mcp__gateway-prod__paper2slides-*"
    ]
  }
}
```

These are merged during `agentihooks init` on top of the base settings.

## Bundle-based approach

Instead of linking individual connectors, put all your customizations in a bundle:

```
my-tools/
├── .claude/
│   ├── .mcp.json              # Bundle MCP servers
│   ├── skills/
│   ├── agents/
│   ├── commands/
│   └── rules/
└── profiles/
    ├── default/
    │   ├── CLAUDE.md
    │   ├── profile.yml
    │   └── .claude/
    │       └── settings.overrides.json   # deny rules for default profile
    └── coding/
        ├── CLAUDE.md
        ├── profile.yml
        └── .claude/
            └── settings.overrides.json   # deny rules for coding profile
```

Install with:

```bash
agentihooks init --bundle ~/dev/my-tools --profile default
```

See [Bundles](bundles.md) for full documentation.
