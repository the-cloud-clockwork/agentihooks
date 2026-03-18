---
title: Cost Management
nav_order: 3
permalink: /docs/cost-management/
---

# Cost Management
{: .no_toc }

**Stop burning through your Claude Code quota.** AgentiHooks ships a full cost management layer that watches every token entering and leaving your context window — and actively intervenes to keep spending under control.

{: .highlight }
> Users report noticeably slower quota consumption after enabling AgentiHooks. Every feature below is **on by default** and works without configuration.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## The problem

Claude Code is powerful — but it's expensive. Without guardrails:

- **Verbose bash output floods the context** — a single `docker logs` or `npm install` can dump 10K+ tokens into the window
- **Redundant file reads waste tokens** — Claude re-reads the same unchanged file 3–5 times per session
- **No visibility into burn rate** — you don't know you've consumed 80% of your context until it's too late and the session resets
- **No plan-level quota awareness** — you hit your weekly limit mid-task with no warning

AgentiHooks solves all four.

---

## What you save

| Feature | What it prevents | Estimated token savings |
|---------|-----------------|------------------------|
| **Bash output filtering** | Verbose docker/kubectl/git/test/build output flooding context | **5K–50K tokens per command** |
| **File read deduplication** | Claude re-reading the same unchanged file multiple times | **2K–20K tokens per duplicate read** |
| **MCP lazy loading** | 26 MCP tool schemas loaded upfront every turn | **~79K tokens per session** |
| **Context threshold warnings** | Session running to 100% and resetting (losing all context) | **Entire session cost** |
| **MCP hygiene reminders** | Unused MCP servers contributing schema tokens every turn | **10K–100K tokens per session** |

{: .important }
> A single session with all features active can save **100K–250K tokens** compared to vanilla Claude Code. Over a week of heavy use, that's the difference between hitting your quota on Wednesday vs. lasting through Friday.

---

## Feature breakdown

### 1. Real-time cost display

Every turn, your status bar shows exactly what you're spending:

```
████████████░░░ 53% | Sonnet 4.6 | $0.1842 | 1h
ctx: 112K/200K | burn: 8K/turn | +42-12 | cache: 67% | main
```

**Line 1:** Context fill bar with color (green/yellow/red), model, cumulative session cost in USD, session duration.

**Line 2:** Raw token counts, burn rate per turn, lines changed, prompt cache hit ratio, git branch.

No setup required — active by default.

---

### 2. Bash output filtering

Detects verbose command output and truncates it *before* it enters the context window:

| Command type | Detection | Truncation |
|-------------|-----------|------------|
| `docker logs` / `docker compose logs` | Command string match | Last 50 lines (configurable) |
| `kubectl` commands | Command string match | Last 50 lines |
| `git log` | Command string match | Last 20 commits |
| `pytest` / `jest` / `npm test` / `cargo test` | Output pattern match | Last 10 failure blocks |
| `npm install` / `pip install` / `cargo build` | Command string match | Last 5000 chars |
| Everything else | Fallback | Hard cap at 5000 chars |

The filter adds a clear notice when truncating:
```
[truncated: kept last 50 of 2847 lines]
```

Claude sees the most recent, most relevant output — not the entire history.

**Config:**

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `BASH_FILTER_ENABLED` | `true` | Master switch |
| `BASH_FILTER_MAX_LINES` | `50` | Docker/kubectl line limit |
| `BASH_FILTER_MAX_CHARS` | `5000` | Build output char cap |
| `BASH_FILTER_TEST_MAX_FAILURES` | `10` | Test failure block limit |
| `BASH_FILTER_GIT_MAX_COMMITS` | `20` | Git log commit limit |

---

### 3. File read deduplication

Blocks Claude from re-reading files it already has in context — the single biggest source of wasted tokens in long sessions.

**How it works:**

1. On every `Read` tool call, the cache records the file path and its `mtime`
2. If Claude tries to read the same file again, the hook checks whether the file has been modified since
3. **Modified?** Read goes through, mtime is updated
4. **Unchanged?** Read is blocked with a message: *"File already read this session and unchanged on disk"*

The cache uses Redis when available (persists across hook invocations) with an in-memory fallback.

**Config:**

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `FILE_READ_CACHE_ENABLED` | `true` | Master switch |
| `FILE_READ_CACHE_BACKEND` | `redis` | `redis` or `memory` |
| `FILE_READ_CACHE_TTL` | `21600` | Redis key TTL (6 hours) |

---

### 4. Context threshold warnings

Edge-triggered warnings fire exactly once per threshold crossing per session — no spam, no missed alerts:

| Threshold | Default | What happens |
|-----------|---------|-------------|
| **Warning** | 60% | Yellow banner: *"CONTEXT 60% — consider /compact soon"* |
| **Critical** | 80% | Red banner: *"CONTEXT 80% — /compact now or start new session"* |

Warnings appear on statusline Line 3 so they're impossible to miss. Edge-triggering is tracked in Redis — each level fires at most once.

**Config:**

| Variable | Default |
|----------|---------|
| `TOKEN_WARN_PCT` | `60` |
| `TOKEN_CRITICAL_PCT` | `80` |

---

### 5. MCP tool lazy loading

Set `ENABLE_TOOL_SEARCH=true` (default) and all 26 MCP tools load on demand instead of injecting their full JSON schemas into every turn.

**Before:** ~79K tokens of tool schemas loaded upfront, every single turn.
**After:** Tools appear as "(loaded on-demand)" and only expand when Claude actually uses them.

This is set in the `env` block of `settings.json` — the installer configures it automatically.

---

### 6. MCP hygiene reminders

At session start, AgentiHooks injects a reminder prompting Claude to check `/mcp` and disable any MCP servers not needed for the current task. Each disabled server saves its schema tokens on every subsequent turn.

**Config:** `MCP_HYGIENE_ENABLED=true` (default)

---

### 7. Burn rate tracking

Every turn, the statusline computes how many tokens were consumed since the previous turn:

```
burn: 8K/turn
```

This lets you spot runaway token consumption in real time — a `burn: 45K/turn` after a bash command tells you something verbose just hit the context. Requires Redis for cross-turn delta computation; omitted gracefully without it.

---

### 8. Console quota monitoring (opt-in)

A background daemon scrapes your Claude.ai usage page and surfaces plan-level quota on statusline Line 3:

```
quota: session:53% [1h] | weekly: all:35% resets fri 10:00 am | sonnet:5% resets mon 12:00 am | extra: €40/99 (40%) resets apr 1
```

You see your weekly quota percentage, per-model breakdown, extra usage spend, and reset times — all color-coded (green < 60%, yellow < 80%, red above).

**Setup:**

```bash
# One-time: install headless browser
~/.agentihooks/.venv/bin/python -m playwright install chromium

# Authenticate (opens your browser, paste sessionKey cookie)
agentihooks quota auth

# Enable in ~/.agentihooks/.env
echo 'CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json' >> ~/.agentihooks/.env
```

**Config:**

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `CLAUDE_USAGE_FILE` | — | Path to quota JSON (enables the feature) |
| `CLAUDE_USAGE_POLL_SEC` | `60` | Daemon poll interval |
| `CLAUDE_USAGE_STALE_SEC` | `300` | Data staleness threshold |

---

## Everything at a glance

| Layer | Feature | Default | Tokens saved | Config |
|-------|---------|---------|-------------|--------|
| **Output** | Bash output filtering | On | 5K–50K/cmd | `BASH_FILTER_ENABLED` |
| **Input** | File read dedup | On | 2K–20K/read | `FILE_READ_CACHE_ENABLED` |
| **Schema** | MCP lazy loading | On | ~79K/session | `ENABLE_TOOL_SEARCH` |
| **Schema** | MCP hygiene reminder | On | 10K–100K/session | `MCP_HYGIENE_ENABLED` |
| **Awareness** | Statusline cost/burn | On | Prevents waste | `TOKEN_MONITOR_ENABLED` |
| **Awareness** | Context warnings | On | Prevents resets | `TOKEN_WARN_PCT` / `TOKEN_CRITICAL_PCT` |
| **Awareness** | Console quota display | Opt-in | Prevents limit hits | `CLAUDE_USAGE_FILE` |

**Master switch:** Set `TOKEN_CONTROL_ENABLED=false` to disable all token control features at once. Individual features can be toggled independently.

---

## Quick start

Everything except quota monitoring works out of the box after installation:

```bash
# Install agentihooks — all cost features are enabled by default
~/.agentihooks/.venv/bin/python scripts/install.py global
```

To add quota monitoring:

```bash
~/.agentihooks/.venv/bin/python -m playwright install chromium
agentihooks quota auth
echo 'CLAUDE_USAGE_FILE=~/.agentihooks/claude_usage.json' >> ~/.agentihooks/.env
```

That's it. Open Claude Code and watch your statusline — you'll see exactly where every token goes.
