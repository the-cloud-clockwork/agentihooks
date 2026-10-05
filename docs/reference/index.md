---
title: Reference
nav_order: 6
has_children: true
---

# Reference

Complete reference documentation for configuration and CLI commands.

## Pages in this section

| Page | What it covers |
|------|---------------|
| [Configuration](configuration.md) | All environment variables across every integration, in one place |
| [CLI Commands](cli-commands.md) | All `agentihooks` subcommands and flags: init, uninstall, claude, ignore |
| [Observability with Langfuse](observability-langfuse.md) | Every telemetry path and where it goes, turning Langfuse on per profile, other OTLP backends, checking a trace arrived, how the Doctor reads traces |
| [WSL disk reclaim](wsl-disk.md) | Sparse VHDX so space `agentihooks gc` frees returns to Windows; why `/tmp` empties on WSL |
| [CODEX-COMPAT](CODEX-COMPAT.md) | The `codex` install target: hook contract, surface map, divergences |
| [COPILOT-COMPAT](COPILOT-COMPAT.md) | The `copilot` install target: hook contract, surface map, divergences |
