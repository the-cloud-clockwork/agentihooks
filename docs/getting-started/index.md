---
title: Getting Started
nav_order: 2
has_children: true
permalink: /docs/getting-started/
---

# Getting Started

AgentiHooks is a lifecycle hook system and MCP tool server for [Claude Code](https://github.com/anthropics/claude-code) agents.

## What you get

- **[Cost management](../cost-management/)** -- slash your token burn with output filtering, file read dedup, lazy MCP loading, context warnings, and live quota tracking. All on by default. [See what you save ->](../cost-management/)
- **10 lifecycle hooks** -- intercept every Claude Code event (SessionStart, PreToolUse, Stop, etc.) to log transcripts, inject context, scan for secrets, and learn from tool errors
- **26 MCP tools** across 8 categories -- AWS, email, messaging, storage, database, compute, observability, and utilities
- **Profile system** -- swap agent personalities, skills, and tool access by choosing a profile at install time
- **3-layer asset merge** -- agentihooks built-in, bundle global, and profile-specific skills/agents/commands/rules
- **Multi-account quota monitoring** -- track plan-level usage across multiple Claude.ai accounts
- **Sync daemon** -- auto-propagates new skills, agents, commands, and rules within 60s
- **Cross-session memory** -- tool error patterns persist across sessions via NDJSON store

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Claude Code CLI

## Pages in this section

| Page | What it covers |
|------|---------------|
| [Installation](installation.md) | Install agentihooks globally into `~/.claude` |
| [Profiles](profiles.md) | Choose and switch agent profiles, 3-layer merge |
| [Per-Project Configuration](per-project.md) | `.agentihooks.json`, local profiles, MCP whitelists, CLAUDE.local.md |
| [Portability & Reusability](portability.md) | Move setups across machines, manage credentials |
