---
title: Cost Management
parent: Reference
nav_order: 9
---

# Cost Management
{: .no_toc }

**Stop burning through your Claude Code quota.** AgentiHooks ships a full cost management layer that watches every token entering and leaving your context window -- and actively intervenes to keep spending under control.

{: .highlight }
> Users report noticeably slower quota consumption after enabling AgentiHooks. Every feature below is **on by default** and works without configuration.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## The problem

Claude Code is powerful -- but it's expensive. Without guardrails:

- **Verbose bash output floods the context** -- a single `docker logs` or `npm install` can dump 10K+ tokens into the window
- **Redundant file reads waste tokens** -- Claude re-reads the same unchanged file 3-5 times per session
- **No visibility into burn rate** -- you don't know you've consumed 80% of your context until it's too late and the session resets
- **No plan-level quota awareness** -- you hit your weekly limit mid-task with no warning

AgentiHooks solves all four.

---

## What you save

| Feature | What it prevents | Estimated token savings |
|---------|-----------------|------------------------|
| **Bash output filtering** | Verbose docker/kubectl/git/test/build output flooding context | **5K-50K tokens per command** |
| **File read deduplication** | Claude re-reading the same unchanged file multiple times | **2K-20K tokens per duplicate read** |
| **MCP lazy loading** | 26 MCP tool schemas loaded upfront every turn | **~79K tokens per session** |
| **Smart compact suggestions** | Generic "/compact" warnings that don't tell you what to drop | **Faster, more effective compaction** |
| **Context audit tracking** | No visibility into what tools consume the most context | **Informed compaction decisions** |
| **Thinking/effort policy** | Extended thinking burning tens of thousands of output tokens | **10K-50K tokens per over-think** |
| **Peak hour awareness** | Running expensive jobs during peak billing hours | **Session budget preservation** |
| **MCP surface area reporting** | Heavy MCP servers silently consuming context every turn | **10K-100K tokens per session** |
| **CLAUDE.md linting** | Bloated CLAUDE.md paying tokens on every turn | **500-5K tokens per turn** |
| **MCP hygiene reminders** | Unused MCP servers contributing schema tokens every turn | **10K-100K tokens per session** |

{: .important }
> A single session with all features active can save **100K-250K tokens** compared to vanilla Claude Code. Over a week of heavy use, that's the difference between hitting your quota on Wednesday vs. lasting through Friday.

---

## Feature breakdown

### 1. Real-time cost display

Every turn, your status bar shows exactly what you're spending:

```
############ 53% | Sonnet 4.6 | $0.1842 | 1h
ctx: 112K/200K | burn: 8K/turn | +42-12 | cache: 67% | main
```

**Line 1:** Context fill bar with color (green/yellow/red), model, cumulative session cost in USD, session duration.

**Line 2:** Raw token counts, burn rate per turn, lines changed, prompt cache hit ratio, git branch.

No setup required -- active by default.

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

Claude sees the most recent, most relevant output -- not the entire history.

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

Blocks Claude from re-reading files it already has in context -- the single biggest source of wasted tokens in long sessions.

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

Edge-triggered warnings fire exactly once per threshold crossing per session -- no spam, no missed alerts:

| Threshold | Default | What happens |
|-----------|---------|-------------|
| **Warning** | 60% | Yellow banner: *"CONTEXT 60% -- consider /compact soon"* |
| **Critical** | 80% | Red banner: *"CONTEXT 80% -- /compact now or start new session"* |

Warnings appear on the statusline's conditional Line 4 (the row that also carries native rate limits and the peak/off-peak indicator) so they're impossible to miss. Line 3 carries the agentihooks profile + settings-profile + channels list. Edge-triggering is tracked in Redis -- each level fires at most once.

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

This is set in the `env` block of `settings.json` -- the installer configures it automatically.

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

This lets you spot runaway token consumption in real time -- a `burn: 45K/turn` after a bash command tells you something verbose just hit the context. Requires Redis for cross-turn delta computation; omitted gracefully without it.

---

### 8. Native rate limit display

Claude Code provides native rate limit data (`rate_limits.five_hour` and `rate_limits.seven_day`) in every statusline payload. AgentiHooks surfaces this on statusline Line 4 (the conditional row that also shows context-threshold warnings and the peak/off-peak indicator; Line 3 above it carries the agentihooks profile + channels list):

```
session:53% [1h35m] | weekly:35%
```

You see your session and weekly quota percentages with reset countdowns -- all color-coded (green < 60%, yellow < 80%, red above). No configuration required -- this works out of the box with Claude Code's built-in rate limit tracking.

---

### 9. Context audit tracking

Tracks cumulative byte output per tool type across the session. When context fill exceeds the audit threshold on Stop, a report is logged showing the top 5 consumers.

```
Context audit (fill: 82%, total tool output: 245K):
  Read: 120K (49%)
  Bash: 65K (27%)
  Agent: 38K (16%)
  Edit: 12K (5%)
  Grep: 10K (4%)
```

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `CONTEXT_AUDIT_ENABLED` | `true` | Enable per-tool tracking |
| `CONTEXT_AUDIT_THRESHOLD_PCT` | `70` | Emit report when fill exceeds this % |

---

### 10. Smart compact suggestions

Replaces generic "/compact" warnings with actionable suggestions based on context audit data:

```
CONTEXT 65% -- consider /compact soon -- top consumers: Read (50K), Bash (32K), Agent (28K)
```

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `COMPACT_SUGGEST_ENABLED` | `true` | Use smart suggestions vs generic warnings |

---

### 11. Thinking/effort policy

Injects effort guidance at session start based on profile settings. Warns when subagents are spawned with unnecessarily expensive models.

```
TOKEN EFFICIENCY: Default effort: medium. Reserve high/ultrathink for complex
architectural decisions. Prefer Sonnet for implementation; reserve Opus for planning.
```

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `EFFORT_POLICY_ENABLED` | `true` | Inject effort guidance at session start |
| `DEFAULT_EFFORT` | `medium` | Default reasoning depth (low/medium/high) |
| `THINKING_BUDGET_TOKENS` | `0` | Advisory token ceiling (0 = no limit) |

---

### 12. Peak/off-peak awareness

Detects Anthropic's peak billing hours (weekday business hours US Pacific) and shows an indicator on the statusline. When session usage is high during peak hours, adds a warning.

```
session:62% [1h] | PEAK -- sessions burn faster during business hours
```

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `PEAK_HOURS_ENABLED` | `true` | Show peak indicator on statusline |
| `PEAK_HOURS_START` | `9` | Peak start hour |
| `PEAK_HOURS_END` | `17` | Peak end hour |
| `PEAK_HOURS_TZ` | `US/Pacific` | Timezone for peak calculation |

---

### 13. MCP surface area reporting

CLI tool that analyzes MCP server configurations and reports estimated token overhead. Also warns at session start if total tools exceed a threshold.

```bash
agentihooks mcp report
```

```
MCP Surface Area Report
Total: 9 servers, ~112 tools, ~16,800 schema tokens

Server                         Source   Tools   ~Tokens
hooks-utils                      user      32     4,800
github                           user      40     6,000
...
```

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `MCP_TOOL_WARN_THRESHOLD` | `40` | Warn at session start if total tools exceed this |
| `MCP_SCHEMA_AVG_TOKENS` | `150` | Estimated tokens per tool schema |

---

### 14. CLAUDE.md linting and skill extraction

CLI tool that analyzes CLAUDE.md token cost and suggests extracting workflow-specific sections into on-demand skills.

```bash
agentihooks lint-claude                           # analyze ~/.claude/CLAUDE.md
agentihooks extract-skill "Commands" --name cmds  # extract to skill
```

Moving workflow-specific content from CLAUDE.md (loaded every turn) to skills (loaded on demand) reduces base context cost by 500-5K tokens per turn.

---

## Everything at a glance

| Layer | Feature | Default | Tokens saved | Config |
|-------|---------|---------|-------------|--------|
| **Output** | Bash output filtering | On | 5K-50K/cmd | `BASH_FILTER_ENABLED` |
| **Input** | File read dedup | On | 2K-20K/read | `FILE_READ_CACHE_ENABLED` |
| **Schema** | MCP lazy loading | On | ~79K/session | `ENABLE_TOOL_SEARCH` |
| **Schema** | MCP hygiene reminder | On | 10K-100K/session | `MCP_HYGIENE_ENABLED` |
| **Schema** | MCP surface area reporting | On | 10K-100K/session | `MCP_TOOL_WARN_THRESHOLD` |
| **Awareness** | Statusline cost/burn | On | Prevents waste | `TOKEN_MONITOR_ENABLED` |
| **Awareness** | Context warnings (smart) | On | Prevents resets | `COMPACT_SUGGEST_ENABLED` |
| **Awareness** | Context audit | On | Informed compaction | `CONTEXT_AUDIT_ENABLED` |
| **Awareness** | Peak hour indicator | On | Budget preservation | `PEAK_HOURS_ENABLED` |
| **Awareness** | Native rate limit display | On | Prevents limit hits | *(native)* |
| **Decode** | Thinking/effort policy | On | 10K-50K/over-think | `EFFORT_POLICY_ENABLED` |
| **Startup** | CLAUDE.md linting | CLI | 500-5K/turn | `agentihooks lint-claude` |

**Master switch:** Set `TOKEN_CONTROL_ENABLED=false` to disable all token control features at once. Individual features can be toggled independently.

---

## Quick start

Everything works out of the box after installation:

```bash
# Install agentihooks -- all cost features are enabled by default
agentihooks init
```

Verify everything is working:

```bash
agentihooks status
```

This shows your full system health: profile, hooks, Python, Redis, OTEL, all cost guardrails with descriptions, your entire MCP fleet with real tool counts (queried via MCP protocol, cached 1h), per-project enabled/disabled state, and rate limit summary with peak/off-peak indicator.

Inside a Claude session, use `/agentihooks` for the same diagnostics plus live session metrics (context fill, burn rate, per-tool consumption).
