### Fixed

- `agentihooks init --target codex` now removes MCP servers from `~/.codex/config.toml` that an earlier install wrote and the profile chain no longer ships. Servers the operator added stay, and nothing is removed in a run where a profile of the chain did not resolve.
- Re-registering a target's global install keeps the MCP ledger its adapter recorded in the same run, so codex and copilot teardown can find the servers they wrote.
