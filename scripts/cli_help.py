USAGE = """
Quick start:
    agentihooks init --bundle /path/to/bundle --profile colt
    agentihooks init --profile colt          # re-run (bundle already linked)

Commands:
    agentihooks init [--bundle PATH] [--profile NAME]
        First-time setup or re-install. Links a bundle, selects a profile,
        and installs everything into ~/.claude: hooks, settings, skills,
        agents, commands, rules, CLAUDE.md, and MCP servers.

    agentihooks uninstall [--yes]
        Remove all agentihooks artifacts: symlinks, settings, CLAUDE.md,
        MCP servers, and the CLI. Preserves ~/.agentihooks/state.json.

    agentihooks prune [-v]
        Remove stale MCP entries from disabledMcpServers and known-mcp-servers.json.

    agentihooks ignore [path] [--force]
        Create a .claudeignore in the current directory.

    agentihooks claude [--route SLUG] [extra flags]
        Route to the OAuth account with a free place under its five hour session band
        and the fewest live sessions, and launch Claude.
        --route SLUG selects AH_CC_TOKEN_<SLUG> directly, ignoring the band.
        Alias: agenti (added to ~/.bashrc by init)

    agentihooks codex [--route SLUG] [codex args]
        Route to an AH_CX_TOKEN account (the default login only when none is set) and launch Codex.

    agentihooks codex login <slug> [--device-auth] [--openbao|--no-openbao]
        Log in a ChatGPT workspace in its own Codex folder, write AH_CX_TOKEN_<slug> and
        AGENTIHOOKS_AICODE_TOKEN_REFRESH=true into the managed token block, and optionally
        store the token in OpenBao secret/agentihooks/codex-creds.

    agentihooks codex logout <slug> [--openbao|--no-openbao]
        Remove AH_CX_TOKEN_<slug> and AH_CX_REFRESH_<slug> from OpenBao, the account's Codex
        folder, and its export from the managed token block.

    agentihooks codex refresh [--force SLUG] [--days N]
        Renew Codex access tokens on the refresh authority (AGENTIHOOKS_AICODE_TOKEN_REFRESH=true;
        store AGENTIHOOKS_AICODE_TOKEN_STORE=local|openbao).

    agentihooks claude login [slug] [--openbao|--no-openbao]
        Capture the one-year token from claude setup-token, write AH_CC_TOKEN_<slug>, and
        optionally store it in OpenBao secret/agentihooks/claude-creds.

        Slugs: use the account email; lowercase, @ becomes at, every other symbol becomes _;
        no leading _, and not api, default, help or status.

    agentihooks init-agent [launcher options] [--handoff] -- [claude flags]
        Open a routed Claude session in a new terminal on WSL, macOS, or Linux.
        --handoff moves this session's work to another account (quota handoff).

    agentihooks herdr [status|install|configure|enable|disable]
        Install and configure herdr, the terminal host init-agent opens agents in.

    agentihooks quota [--json] [--refresh]
        One table for every Claude and Codex account: harness, state, sessions, cap,
        routing left and the five hour and weekly windows.

    agentihooks balance --dry-run [--fable] [--refresh]
        One table for every Claude and Codex account, as quota, without launching anything.

    agentihooks conditions list [--step pre|post|stop] [--tool NAME] [--command CMD]
        Show bundle and profile conditions, invalid files, and what fires for a call.

    agentihooks --list-profiles     # show available profiles
    agentihooks --query             # print active profile name

Profile layout (mirrors Claude Code project structure):
    profiles/<name>/
    ├── CLAUDE.md                    # system prompt (→ ~/.claude/CLAUDE.md)
    └── .claude/
        ├── settings.overrides.json  # merged into ~/.claude/settings.json
        ├── .mcp.json                # profile MCP servers
        ├── skills/                  # → ~/.claude/skills/
        ├── agents/                  # → ~/.claude/agents/
        ├── commands/                # → ~/.claude/commands/
        ├── rules/                   # → ~/.claude/rules/
        └── conditions/              # run in place on matching tool calls

3-layer merge: agentihooks built-in → bundle global → profile-specific.
The built-in layer (skills/agents/commands/rules) lives under profiles/package/
— the packaged emulation of a .claude/ tree, shipped in the wheel.
All commands are idempotent. Data directory: ~/.agentihooks/
"""
