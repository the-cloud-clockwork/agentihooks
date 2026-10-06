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
| [Decision classifier](classifier.md) | `hooks.classifier`: typed questions to the LiteLLM decision models, failover, down cache, decision log, `classify` and `classifier stats` |
| [Observability with Langfuse](observability-langfuse.md) | Every telemetry path and where it goes, turning Langfuse on per profile, other OTLP backends, checking a trace arrived, how the Doctor reads traces |
| [Presentation layer architecture](presentation-layer.md) | The ledger pages, HOME and BIN as a product: constraints, today's architecture, limits, target (static pages, `/api/v1`, server-sent events, SQLite behind a repository) and the migration order |
| [Ledger design with Impeccable live](ledger-design-live.md) | Start an Impeccable live session against a scratch ledger server; the rules that keep it away from the shared server |
| [WSL disk reclaim](wsl-disk.md) | Sparse VHDX so space `agentihooks gc` frees returns to Windows; why `/tmp` empties on WSL |
| [CODEX-COMPAT](CODEX-COMPAT.md) | The `codex` install target: hook contract, surface map, divergences |
| [COPILOT-COMPAT](COPILOT-COMPAT.md) | The `copilot` install target: hook contract, surface map, divergences |
