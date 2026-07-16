---
title: Profiles
nav_order: 3
parent: Getting Started
---

# Profiles
{: .no_toc }

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## What is a profile?

A **profile** is a named configuration bundle that controls:

- Which **agent system prompt** (`CLAUDE.md`) Claude Code loads
- Which **settings overrides** are applied (permissions, env vars)
- Which **MCP tool categories** are enabled (via `MCP_CATEGORIES`)
- Which **skills, agents, commands, and rules** are symlinked
- Model selection, turn limits, permission mode, and timeout settings (via `agentihooks claude`)

Profiles are stored under `profiles/<name>/` in the repository or in a linked bundle.

---

## Profile structure

```
profiles/
├── _base/
│   └── settings.base.json          # Canonical settings (hooks, permissions, MCP servers)
├── default/
│   ├── CLAUDE.md                    # Agent system prompt (at profile ROOT)
│   └── .claude/
│       ├── settings.overrides.json  # Per-profile settings overrides
│       ├── .mcp.json                # Profile MCP servers
│       ├── skills/                  # Profile-specific skills
│       ├── agents/                  # Profile-specific agents
│       ├── commands/                # Profile-specific commands
│       └── rules/                   # Profile-specific rules
└── coding/
    └── ...                          # Same structure
```

### `_base/settings.base.json`

This is the **single source of truth** for all settings. It contains:

- Hook event wiring (`hooks` -> shell commands)
- Tool permission allowances
- MCP server definitions

All paths use `/app` as a placeholder. The install script substitutes `/app` with the real repo path at render time.

### `CLAUDE.md` (at profile root)

The agent's system prompt. This file lives at the **profile root** (not inside `.claude/`). The install script copies it to `~/.claude/CLAUDE.md` (as a real file, not a symlink, for WSL/Windows compatibility). In chain mode, multiple profiles' CLAUDE.md files are concatenated.

### `.claude/settings.overrides.json`

Per-profile settings overrides that are merged on top of `_base/settings.base.json` during install. Lives inside the `.claude/` subdirectory.

The `env` block is significant — values land in `~/.claude/settings.json` after `agentihooks init`, then Claude Code injects them into every hook subprocess. This is how feature flags and channel subscriptions reach the runtime without code changes:

```json
{
  "env": {
    "AGENTIHOOKS_SECRETS_MODE": "standard",
    "AGENTIHOOKS_BASE_CHANNELS": "brain,amygdala",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false"
  },
  "permissions": {
    "defaultMode": "auto"
  }
}
```

Layering for the `env` block follows Claude Code's native settings hierarchy: profile default (this file) → repo `.claude/settings.json` → repo `.claude/settings.local.json` → container launch ENV (highest). See [Broadcast System → Channel Subscriptions](../hooks/broadcast.md#channel-subscriptions) for a worked example with `AGENTIHOOKS_BASE_CHANNELS`.

> `profile.yml` was removed 2026-05-07. A profile is now its directory plus
> `CLAUDE.md` and `.claude/`. The launcher `agentihooks claude` passes only
> `--dangerously-skip-permissions`; everything else is set in
> `.claude/settings.overrides.json`.

---

## 3-layer merge

When `agentihooks init` runs, skills, agents, commands, rules, and MCP servers are merged from three layers:

1. **agentihooks built-in** -- `.claude/` in the agentihooks repo
2. **Bundle global** -- `.claude/` in the linked bundle root
3. **Profile-specific** -- `profiles/<name>/.claude/`

Later layers override earlier ones. This lets you start with a shared base, add team customizations via the bundle, and fine-tune per profile.

---

## Built-in profiles

| Profile | Mode | Secrets | Deny | Ask | Best for |
|---------|------|---------|------|-----|----------|
| `default` | `auto` | `standard` | Push to main/master, force push | *(empty)* | General use — autonomous but protected branches are sacred |
| `coding` | `acceptEdits` | `strict` | Protected branch pushes, merge, gh CLI | git push, rm -rf, docker, kubectl | Feature branch development, safe coding |
| `admin` | `bypassPermissions` | `warn` | *(none)* | *(empty)* | Infrastructure, admin tasks, full trust |

These are **settings profiles** — they control permissions and tool access. Combine them with any **persona profile** (which controls rules, CLAUDE.md, behavioral instructions) using the [two-axis model](#settings-profiles--independent-settings-layer).

---

## Listing profiles

```bash
agentihooks --list-profiles
```

Example output:

```
Available profiles:
  default
  coding
  admin
```

Profiles from both the agentihooks repo and linked bundle are listed.

---

## Profile resolution order

When `agentihooks init` runs, the profile is resolved through this precedence chain:

1. **`--profile` CLI flag** — explicit selection
2. **`AGENTIHOOKS_PROFILE` env var** — useful in CI/Docker
3. **`state.json` → `targets.global.profile`** — previous install remembered
4. **Interactive prompt** (if TTY) — defaults to `default` on empty input
5. **Hardcoded `default`** (non-interactive) — headless fallback

If `--profile` is passed with an empty value (`--profile ""`), it is treated as unset and falls through to step 2+. This means **the `default` profile is always the final fallback** — no profile flag, empty profile flag, or missing env var all resolve to `default`.

```bash
agentihooks init                    # → resolves to "default" (or previous install)
agentihooks init --profile ""       # → resolves to "default" (empty = unset)
AGENTIHOOKS_PROFILE= agentihooks init  # → resolves to "default"
```

The `default` profile ships with agentihooks at `profiles/default/`. Bundle profiles (in a linked bundle's `profiles/` directory) are also discoverable — built-in profiles are checked first, then bundle profiles.

---

## Switching profiles

Re-run init with `--profile`:

```bash
agentihooks init --profile coding
```

Or set the `AGENTIHOOKS_PROFILE` environment variable so you don't have to pass `--profile` every time:

```bash
export AGENTIHOOKS_PROFILE=coding
agentihooks init
```

This is especially useful in CI/Docker automation where the profile is set once in the container environment.

Either way, the command atomically:
1. Writes `~/.claude/CLAUDE.md` (marked copy for single profile, marked concatenation for chains)
2. Updates `MCP_CATEGORIES` in the hook environment
3. Re-merges settings overrides (sequential deep merge across all chained profiles)
4. Re-symlinks skills, agents, commands, and rules (3-layer merge, additive across chain)
5. Re-merges MCP servers (additive across chain)

The switch takes effect on the next Claude Code session.

---

## Settings profiles — independent settings layer

Sometimes you want to change **what tools and permissions are available** without changing **who the AI is**. The `--settings-profile` flag provides a second axis:

```bash
# Full install: Anton persona + admin settings overlay
agentihooks init --profile anton --settings-profile admin

# Quick switch: change settings layer only, keep persona intact
agentihooks settings-profile admin

# Revert to persona defaults
agentihooks settings-profile --clear
```

### Two-axis model

| Axis | Controls | Source files |
|------|----------|-------------|
| **Persona** (`--profile`) | Rules, CLAUDE.md, skills, agents, commands | `CLAUDE.md`, `.claude/rules/`, `.claude/skills/`, etc. |
| **Settings** (`--settings-profile`) | Permissions, env vars, tool allowlists, MCP | `settings.overrides.json`, `.claude/.mcp.json` |

The settings profile overlay is applied **after** the persona profile's settings overrides, so it wins on conflicts. Only `settings.overrides.json` and `.mcp.json` are read from the settings profile — rules, CLAUDE.md, skills, agents, and commands are ignored.

### Environment variable

```bash
export AGENTIHOOKS_SETTINGS_PROFILE=admin
agentihooks init --profile anton  # automatically uses admin settings overlay
```

### State tracking

Both axes are persisted in `~/.agentihooks/state.json`:

```json
{
  "targets": {
    "global": {
      "profile": "anton",
      "settings_profile": "admin"
    }
  }
}
```

Re-run `agentihooks init` after editing profile directories to re-apply the layered merge.

---

## Querying the active profile

```bash
agentihooks --query
```

Reports the active global profile chain:

```
chain: [anton, brain] (global)
```

---

## Launching Claude

The `agentihooks claude` command (alias: `agenti`) launches Claude Code with `--dangerously-skip-permissions` and passes through any extra args:

```bash
agentihooks claude           # claude --dangerously-skip-permissions
agenti                       # same thing (alias installed by init)
agenti --model haiku         # extras passed through
```

Model, effort, and other Claude Code defaults come from `~/.claude/settings.json` (rendered from each profile's `settings.overrides.json`).

---

## Creating a custom profile

1. Copy an existing profile:
   ```bash
   cp -r profiles/default profiles/myprofile
   ```

2. Edit `profiles/myprofile/CLAUDE.md` with your custom system prompt.

3. Optionally add profile-specific assets in `profiles/myprofile/.claude/` (skills, agents, commands, rules, `.mcp.json`, `settings.overrides.json`).

4. Install the new profile:
   ```bash
   agentihooks init --profile myprofile
   ```

{: .note }
Profiles affect the **agent's persona, tool access, and asset selection** but not the underlying hook behavior. Hooks are always wired from `_base/settings.base.json` regardless of profile.

---

## Profile chaining

You can combine multiple profiles by separating them with commas:

```bash
agentihooks init --profile coding,anton
```

Profiles are applied **left to right** — the last profile has highest priority for simple values, and all profiles contribute additively for hooks, rules, skills, agents, commands, and MCP servers.

### How chaining works

Given `--profile coding,anton`:

```
settings.base.json                    ← base hooks + defaults
        ↓
coding/settings.overrides.json        ← merged on top (dicts merge, hooks append)
        ↓
anton/settings.overrides.json          ← merged on top (dicts merge, hooks append)
        ↓
~/.claude/settings.json               ← final result
```

### Per-entity behavior in chains

| Entity | Chain behavior |
|--------|---------------|
| **Settings** | Sequential deep merge — each profile merges on top. Dicts combine, hooks append, simple values = last wins. |
| **Rules/Skills/Agents/Commands** | Additive across all profiles. Same filename = later profile wins. |
| **CLAUDE.md** | **Concatenated** into one file with `---` separators and `<!-- profile: name -->` markers. All profiles' system prompts are active. |
| **MCP servers** | Additive — all profiles' servers accumulate. |
| **OTEL** | Last profile in chain provides these. |

### CLAUDE.md concatenation

In chain mode, all profiles' `CLAUDE.md` files are concatenated into a single real file at `~/.claude/CLAUDE.md`. Each section is marked with an HTML comment:

```markdown
<!-- profile: coding -->
# Coding Agent
...

---

<!-- profile: anton -->
# Anton Profile
...
```

Claude Code loads the entire file as its system prompt, so instructions from all chained profiles are active simultaneously.

{: .note }
In single-profile mode, `CLAUDE.md` is the source file prefixed with its own `<!-- profile: name -->` marker (no `---` separator, since there's nothing to concatenate). In chain mode, it's a rendered concatenation of every profile's marker + content. Both are real files (not symlinks) for WSL/Windows compatibility. Re-run `agentihooks init` to refresh after editing profile sources.

### Querying the active chain

```bash
agentihooks --query
```

Single profile output:
```
anton
```

Chain output:
```
chain: [coding, anton]
```

### Example: combining a coding base with operator customizations

```bash
agentihooks init --profile coding,anton
```

This gives you:
- **coding's** git safety rules, retry breaker, security rules
- **anton's** operator behavioral model, delegation map, response template
- Both profiles' settings merged (hooks appended, env vars combined)
- Both profiles' rules, skills, agents, commands all present

### Linking external profile dirs into the chain

Profiles outside the agentihooks repo and the linked bundle can be added to the chain via `link-profile`:

```bash
# One-shot: register an external dir, append to chain, re-install
agentihooks link-profile link ~/dev/brain-profile

# If the dir basename collides with a built-in or bundle name, alias it
agentihooks link-profile link ~/dev/anton-fork --name anton2

# Inspect linked entries
agentihooks link-profile list

# Remove from chain (sweeps any orphan symlinks pointing into the linked dir)
agentihooks link-profile unlink brain-profile
```

By default `link` auto-appends the new name to the active chain and re-runs `agentihooks init`, so a single command takes a chain from `anton` to `anton,brain-profile` end-to-end. Use `--no-append` to register the path without modifying the chain, or `--no-init` to skip the immediate re-install. Built-in/bundle names always win on collision — the link is rejected unless you pass `--name <alias>`. Linked dirs whose path is later deleted from disk are warn-skipped from the chain on the next `agentihooks init`, with a hint pointing at `link-profile unlink`. See `agentihooks link-profile` in the [CLI reference](../reference/cli-commands.md#agentihooks-link-profile) for full flag and state details.
