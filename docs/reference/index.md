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
| [Delivery tiers](delivery-tiers.md) | The three CI tiers (pull request, queue, promotion), why a red integration branch keeps merging, promotion owning the slow checks, fixer tasks for a red promotion, queueing a green head without merging `dev` first, and where coverage is graded |
| [Test count floor](test-count-floor.md) | The `test-count` job: the base revision grades, the floor, lost tests admitted when the head deletes the function, class, constant or keyword branch they referenced, limits and proof |
| [Promotion and releases](promotion.md) | How a green `dev` push run of Tests rebase merges its graded revision into `main` through a snapshot branch, tags a patch release with generated notes, what blocks it, reruns and manual `release.yml` runs |
| [CI result cache and in place retry](ci-result-cache.md) | The design for one CI algorithm: reuse a check's green result while its real inputs are unchanged, retry a failed test in place, before numbers and slices |
| [WSL disk reclaim](wsl-disk.md) | Sparse VHDX so space `agentihooks gc` frees returns to Windows; why `/tmp` empties on WSL |
| [CODEX-COMPAT](CODEX-COMPAT.md) | The `codex` install target: hook contract, surface map, divergences |
| [Managed Codex terminals](codex-managed-terminal.md) | Transport selection, herdr viewer reopening, push master inbox duties, handoffs and attachment failures |
| [Codex inbox transport](codex-inbox-transport.md) | The `app_server` default, the Claude inbox channel against broadcast topics and Bun plugins, qualified versions, canary order, failure diagnosis and rollback |
| [COPILOT-COMPAT](COPILOT-COMPAT.md) | The `copilot` install target: hook contract, surface map, divergences |
