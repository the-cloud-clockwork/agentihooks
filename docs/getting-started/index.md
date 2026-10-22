---
title: Getting Started
nav_order: 2
has_children: true
---

# Getting Started

AgentiHooks is a lifecycle hook system and MCP tool server for **Claude Code and Codex** agents. Start with the [Docker Compose bundle quick start](compose-quick-start.md), or use [native installation](installation.md) for standalone sessions.

## What you get

- Profiles, shared instructions and skills for Claude Code and Codex, with native settings for each harness.
- Lifecycle hooks for guardrails, context delivery and tool error memory.
- MCP tools for channels, enforcement, conditions and scheduling.
- Optional broadcasts and targeted channels for sessions sharing the broadcast store.
- Optional swarm services for task coordination, durable inbox messages, quota routing and handoffs.

Harness capabilities differ. See the [Codex reference](../reference/CODEX-COMPAT.md) for hook delivery, permissions and configuration limits.

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Claude Code CLI or Codex CLI for native agent sessions; Docker with the Compose plugin for the service quick start

## Pages in this section

| Page | What it covers |
|------|---------------|
| [Docker Compose quick start](compose-quick-start.md) | Start the packaged swarm services and connect clients |
| [Installation](installation.md) | Install native Claude Code and Codex hooks; [synchronize editable tool dependencies](installation.md#synchronize-an-editable-tool-installation) |
| [Profiles](profiles.md) | Choose and switch agent profiles, 3-layer merge |
| [Portability & Reusability](portability.md) | Move setups across machines, manage credentials |
