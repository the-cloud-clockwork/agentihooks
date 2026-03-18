---
title: Getting Started
nav_order: 2
has_children: true
permalink: /docs/getting-started/
---

# Getting Started

AgentiHooks is a lifecycle hook system and MCP tool server for [Claude Code](https://github.com/anthropics/claude-code) agents.

## What you get

- **10 lifecycle hooks** — intercept every Claude Code event (SessionStart, PreToolUse, Stop, etc.) to log transcripts, inject context, scan for secrets, and learn from tool errors
- **26 MCP tools** across 8 categories — AWS, email, messaging, storage, database, compute, observability, and utilities
- **Profile system** — swap agent personalities by choosing a profile at install time
- **Cross-session memory** — tool error patterns persist across sessions via NDJSON store

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Claude Code CLI

## Pages in this section

| Page | What it covers |
|------|---------------|
| [Installation](installation.md) | Install agentihooks globally into `~/.claude` |
| [Profiles](profiles.md) | Choose and switch agent profiles |
| [Portability & Reusability](portability.md) | Move setups across machines, link MCPs, manage credentials |
