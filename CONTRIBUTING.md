# Contributing to agentihooks

Thanks for your interest in contributing! agentihooks is a support repo designed to be forked and extended, so contributions that improve the core framework benefit everyone.

## Getting Started

1. Fork the repo
2. Clone your fork
3. Install dependencies: `pip install mcp[cli] PyJWT requests httpx psycopg2-binary`
4. Make your changes
5. Run syntax checks: `python -c "import py_compile; py_compile.compile('your_file.py', doraise=True)"`
6. Submit a pull request

## What to Contribute

- **New MCP tool categories** -- add a module to `hooks/mcp/`, register in `_registry.py`
- **New integrations** -- add a client to `hooks/integrations/`
- **New hook handlers** -- extend `hooks/hook_manager.py`
- **Bug fixes** -- always welcome
- **Documentation** -- improvements to README, docstrings, or inline comments

## Adding a New MCP Tool Category

1. Create `hooks/mcp/yourcat.py` with a `register(mcp)` function
2. Add the entry to `hooks/mcp/_registry.py`
3. Test with: `MCP_CATEGORIES=yourcat python -m hooks.mcp`

## Code Style

- Use type hints on all public function signatures
- Use lazy imports inside `register()` closures to keep MCP startup fast
- Follow the existing JSON response pattern: `{"success": True/False, ...}`
- Log errors with `from hooks.common import log`

## Testing `scripts/install.py` — read before writing a harness

`install.py` writes to the developer's real machine. Several module-level
constants are bound to real paths **at import time**, so setting an environment
variable after import does nothing — the constant is already pinned. The only
override that works is attribute-patching the already-imported module.

The full list, all of which a harness must redirect before calling
`install_global`, `_install_global_inner`, `cmd_init_unified`, or
`uninstall_global`:

| Attribute | Real target |
|---|---|
| `CLAUDE_HOME` | `~/.claude` |
| `AGENTIHOOKS_STATE_DIR` | `~/.agentihooks` |
| `STATE_JSON` | `~/.agentihooks/state.json` |
| `_CLAUDE_JSON` | `~/.claude.json` |
| `_BASHRC` | `~/.bashrc` |
| `BASE_SETTINGS` | repo `profiles/_base/settings.base.json` |
| `PROFILES_DIR` | repo `profiles/` (needed for synthetic profiles) |
| `AGENTIHOOKS_ROOT` | the repo itself — used as `cwd` for `uv tool install/uninstall` |

`AGENTIHOOKS_ROOT` is the sharp one: **every** `init` shells a real
`uv tool install --editable --force .` gated only by it, and `uninstall_global`
shells a real `uv tool uninstall`. Neither is gated by `CLAUDE_HOME`, so a
harness that redirects only the CLAUDE paths still uninstalls the developer's
CLI for real.

Some call sites cannot be neutralised by attribute-patching at all —
`_migrate_profile_rename` builds `Path.home() / ".claude.json"` as a raw literal
inside the function body, and `cmd_init_unified` calls it unconditionally. Patch
`pathlib.Path.home` itself to cover those.

The suite-wide autouse fixture in `tests/conftest.py` already does all of this and
asserts nothing still resolves under the real home. Use it; don't hand-roll
per-file isolation — that is exactly how the gap reopened twice.

## Pull Request Process

1. Keep PRs focused -- one feature or fix per PR
2. Update the README if you add a new category or change the profile system
3. Add a CHANGELOG entry under `[Unreleased]`
4. Ensure all Python files pass syntax checking

## Commit Messages

Use clear, descriptive commit messages:

```
Add Slack integration MCP tools

New hooks/mcp/slack.py with 3 tools: slack_send_message,
slack_list_channels, slack_upload_file. Registered in _registry.py.
```

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Please be respectful and constructive.

## Questions?

Open a [GitHub Discussion](https://github.com/The-Cloud-Clockwork/agentihooks/discussions) or file an issue.
