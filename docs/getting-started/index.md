---
title: Getting Started
nav_order: 2
has_children: true
---

# Getting Started

AgentiHooks is a lifecycle hook system and MCP tool server for [Claude Code](https://github.com/anthropics/claude-code) agents.

## What you get

- **[Cost management](../cost-management/)** -- slash your token burn with output filtering, file read dedup, lazy MCP loading, context warnings, and native rate limit display. All on by default. [See what you save ->](../cost-management/)
- **10 lifecycle hooks** -- intercept every Claude Code event (SessionStart, PreToolUse, Stop, etc.) to log transcripts, inject context, scan for secrets, and learn from tool errors
- **26 MCP tools** across 7 categories -- AWS, email, storage, database, compute, observability, and utilities
- **Profile system** -- swap agent personalities, skills, and tool access by choosing a profile at install time
- **3-layer asset merge** -- agentihooks built-in, bundle global, and profile-specific skills/agents/commands/rules
- **Cross-session memory** -- tool error patterns persist across sessions via NDJSON store
- **[Broadcast system](../hooks/broadcast.md)** -- send operator messages to all active Claude Code sessions simultaneously; severities: `info` (once, 4h), `alert` (every turn, 1h), `critical` (every turn + every tool call, 30m); compose manually or use `agentihooks broadcast emit "natural language"` for AI-assisted severity selection. Optional channel tagging: messages published to a named channel only reach sessions subscribed via the `AGENTIHOOKS_BASE_CHANNELS` env var (configured per-profile / per-repo / per-container)

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Claude Code CLI

## Pages in this section

| Page | What it covers |
|------|---------------|
| [Installation](installation.md) | Install agentihooks globally into `~/.claude` |
| [Profiles](profiles.md) | Choose and switch agent profiles, 3-layer merge |
| [Portability & Reusability](portability.md) | Move setups across machines, manage credentials |
