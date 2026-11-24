### Added

- `_agentihooks.mcpManualSignIn = true` in a profile's codex native settings writes remote HTTP MCP servers that carry no credential of their own (OAuth sign-in servers) with `enabled = false`, so Codex never tries them at startup or fails with AuthRequired. The operator signs in with `codex mcp login <name>` and sets `enabled = true`, which later installs keep.
