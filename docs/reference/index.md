---
title: Reference
nav_order: 6
has_children: true
---

# Reference

Complete reference documentation for configuration and CLI commands.

See [Focused proof CPU worker limits](../ci-timing.md#focused-proof-cpu-worker-limits) for focused test dispatch, automatic CPU quota and affinity caps, configuration and diagnostics.

## Pages in this section

| Page | What it covers |
|------|---------------|
| [Configuration](configuration.md) | All environment variables across every integration, in one place |
| [Deployed swarm host capacity](../pillars/swarm.md#deployed-host-capacity) | Worker namespace quota and pool budgets, autoscale inspection, configuration and disabled host pressure alerts |
| [CLI Commands](cli-commands.md) | All `agentihooks` subcommands and flags: init, uninstall, claude, ignore |
| [Developer account login](account-login.md) | Claude and Codex login, managed shell tokens, OpenBao paths and key names, refresh authority and token stores |
| [Task scope authorizations](../pillars/swarm.md#task-scope-authorizations) | Record master or operator scope, retrace a plan, interpret authorized pieces and configure enforcement |
| [Decision classifier](classifier.md) | `hooks.classifier`: typed questions to the LiteLLM decision models, failover, down cache, decision log, `classify` and `classifier stats` |
| [Observability with Langfuse](observability-langfuse.md) | Every telemetry path and where it goes, turning Langfuse on per profile, other OTLP backends, checking a trace arrived, how the Doctor reads traces |
| [Presentation layer architecture](presentation-layer.md) | The ledger pages, HOME and BIN as a product: constraints, today's architecture, limits, target (static pages, `/api/v1`, server-sent events, SQLite behind a repository) and the migration order |
| [Ledger watcher replay verification](watch-ledger-replay.md) | Heartbeat frames, replay assertions, test configuration and first-attempt CI evidence |
| [Ledger design with Impeccable live](ledger-design-live.md) | Start an Impeccable live session against a scratch ledger server; the rules that keep it away from the shared server |
| [Hosted ledger auth](hosted-ledger-auth.md) | `SWARM_AUTH_MODE`: local page tokens, or edge with the Cloudflare Access operator and agent tokens from `SWARM_SECRET` |
| [Pull request mutation gate](mutation-gate.md) | Graded lines and bases, [continuation preflight runs](mutation-gate.md#continuation-publication-and-preflight-runs), push preflight reuse, forwarded reports, measured shard costs, split mutants, combined grades, CI proof examples, evidence inspection and survivor clearances |
| [Delivery tiers](delivery-tiers.md) | The three CI tiers (pull request, queue, promotion), why a red integration branch keeps merging, promotion owning the slow checks, fixer tasks for a red promotion, queueing a green head without merging `dev` first, and where coverage is graded |
| [Resource sampling and stop condition checks](../ci-timing.md#resource-sampling-and-stop-condition-regression-checks) | Whole tree resource warning ownership, condition timing budgets, fixture configuration, focused CI proof commands and failure diagnostics |
| [Standing CI budget report](../ci-timing.md#standing-ci-budget-report) | One CI overrun finding, per-stage counts and worst times, openable run links, retention, configuration and watched swarm isolation |
| [Test count floor](test-count-floor.md) | The `test-count` job: the base revision grades, the floor, lost tests admitted when the head deletes the function, class, constant or keyword branch they referenced, limits and proof |
| [Promotion and releases](promotion.md) | How a green `dev` push run of Tests rebase merges its graded revision into `main` through a snapshot branch, tags a patch release with generated notes, what blocks it, reruns and manual `release.yml` runs |
| [CI result cache and in place retry](ci-result-cache.md) | The design for one CI algorithm: reuse a check's green result while its real inputs are unchanged, retry a failed test in place, retry durations downloads and uploads in place, before numbers and slices |
| [Helm kind proof input keys](ci-result-cache.md#helm-kind-proof-input-keys) | Declared chart and image inputs, due decisions, dispatch examples, cache misses and proof measurements |
| [Codex CLI cache in unit jobs](../ci-timing.md#codex-cli-cache) | Seed the CLI on main, configure its release, diagnose cache misses and verify installation and shard timings |
| [Browser cache](browser-cache.md) | Verified Chromium preparation, dev push only saving, restore scope, workflow configuration and cache inspection |
| [WSL disk reclaim](wsl-disk.md) | Sparse VHDX so space `agentihooks gc` frees returns to Windows; why `/tmp` empties on WSL |
| [CODEX-COMPAT](CODEX-COMPAT.md) | The `codex` install target: hook contract, surface map, divergences |
| [Managed Codex terminals](codex-managed-terminal.md) | Installed runtime dependencies, connection verification, transport selection, herdr viewer reopening, push master inbox duties, handoffs and attachment failures |
| [Codex inbox transport](codex-inbox-transport.md) | The `app_server` default, the Claude inbox channel against broadcast topics and Bun plugins, qualified versions, canary order, failure diagnosis and rollback |
| [COPILOT-COMPAT](COPILOT-COMPAT.md) | The `copilot` install target: hook contract, surface map, divergences |
