---
title: "Broadcast Channels + Brain Adapter"
parent: The Four Pillars
nav_order: 6
---

# Broadcast Channels + Brain Adapter
{: .no_toc }

**Targeted messaging for a fleet that thinks.**

> The broadcast system talks to every session. Channels let you whisper to the right ones. The brain adapter pumps knowledge — hot arcs, active context, operational memory — into your agents automatically. Together, they turn a fleet of isolated sessions into a coordinated nervous system.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## The problem

The broadcast system is a megaphone — every session hears every message. That's perfect for incidents and deploy freezes. But a fleet doing real work needs **selective delivery**:

- Your infrastructure agent doesn't need hot arcs about frontend design patterns.
- Your coding agent doesn't need amygdala alarms about database replication lag.
- Your brain-keeper daemon needs to inject knowledge into agents that opted in, not spam the entire fleet.

Channels solve this. And the brain adapter uses channels to inject knowledge from an external brain (Obsidian vault, API, whatever) into the agents that are listening.

---

## Channels

### How channels work

A channel is a named topic. Messages published to a channel only reach sessions that have subscribed to it. Global broadcasts (no channel) still reach everyone — full backward compatibility.

```
                ┌──────────── channel: "brain" ──────────────┐
                │                                             │
                │  ┌─────────┐    ┌─────────┐                │
                │  │ Session A│    │ Session B│  (subscribed)  │
                │  │ brain ✓  │    │ brain ✓  │                │
                │  └─────────┘    └─────────┘                │
                │                                             │
                │  ┌─────────┐                                │
                │  │ Session C│  (not subscribed → skipped)   │
                │  │ ops ✓    │                                │
                │  └─────────┘                                │
                └─────────────────────────────────────────────┘

                ┌──────────── global (no channel) ───────────┐
                │                                             │
                │  All sessions receive. Always.              │
                └─────────────────────────────────────────────┘
```

### Subscribing

Sessions subscribe to channels via the **`AGENTIHOOKS_BASE_CHANNELS`** env var (comma-separated). The value is read once at `hooks.config` import time and shared between the broadcast filter and the statusline display.

**Layering** (lowest precedence → highest, Claude Code's native settings.json `env` block merge):

1. **Profile default** — `profiles/<name>/.claude/settings.overrides.json` `env` block. The `default` profile ships `"AGENTIHOOKS_BASE_CHANNELS": "brain,amygdala"`. Rendered into `~/.claude/settings.json` by `agentihooks init`.
2. **Per-repo override** — `<repo>/.claude/settings.json` `env` block (committed).
3. **Per-repo local override** — `<repo>/.claude/settings.local.json` `env` block (gitignored, operator-private).
4. **Container launch ENV** — `docker run -e AGENTIHOOKS_BASE_CHANNELS=...` or K8s `env:` block. Wins over everything because it's set before the hook subprocess starts.

```json
// example: <repo>/.claude/settings.local.json
{
  "env": {
    "AGENTIHOOKS_BASE_CHANNELS": "brain,amygdala,deploy"
  }
}
```

**Special values:**
- empty / unset → only receive global broadcasts (messages with no `channel` field)
- `*` → wildcard, receive everything on every channel
- whitespace and duplicates are trimmed/deduped at parse time

Mid-session changes require a session restart — same constraint as `agentihooks refresh-rules`.

### Publishing

Publish to a channel via CLI:

```bash
agentihooks channel publish brain "Hot arcs updated: 3 active" -s info -t 1h
agentihooks channel publish ops-alerts "DB replica lag > 5s" -s alert -t 30m
agentihooks channel publish deploy "Deploy freeze until 3am" -s critical -t 4h
```

Or via MCP tool from inside a session:

```
agent → channel_publish(channel="brain", message="...", severity="info")
```

Or programmatically from any script or daemon:

```python
from hooks.context.broadcast import create_broadcast

create_broadcast(
    message="Hot arcs updated",
    severity="info",
    channel="brain",
    ttl_seconds=3600,
)
```

### Delivery mechanics

Channel filtering is a **read-time filter** — messages are stored once in `broadcast.json`, and each session filters at delivery time based on its subscriptions. No per-session copies. The existing severity tiers apply within channels:

| Severity | Delivery | Channel behavior |
|----------|----------|-----------------|
| `critical` | Every turn + every tool call | Only to subscribed sessions |
| `alert` | Every user turn | Only to subscribed sessions |
| `info` | Once per session | Only to subscribed sessions |

Global broadcasts (no `channel` field) bypass the filter entirely — they reach all sessions regardless of subscriptions. This preserves the existing broadcast behavior as a fleet-wide PA system.

### MCP tools

| Tool | Purpose |
|------|---------|
| `channel_publish(channel, message, severity, ttl)` | Publish to a channel |
| `channel_list()` | List active channels with message counts |
| `channel_subscribe(channel)` | Removed — subscriptions are operator-configured via the `AGENTIHOOKS_BASE_CHANNELS` env var (see [Subscribing](#subscribing) above), not via runtime tool calls. |
| `channel_unsubscribe(channel)` | Removed — same reason. Edit the env var in `settings.json` / `settings.local.json` and restart the session. |

---

## Brain adapter

The brain adapter bridges an external knowledge source to the channel system. It reads content, formats it, and publishes to a broadcast channel. The broadcast system handles delivery. The adapter doesn't inject directly — it's a **source → channel** pump.

### Architecture

```
Source (vault / NFS / API)
    ↓
brain_adapter.py
    reads → diffs → publishes to channel "brain"
    ↓
broadcast.json  (channel: "brain", persistent: true)
    ↓
UserPromptSubmit hook
    filters by subscription → inject_context()
    ↓
Agent sees brain content in context
```

> **Receiving brain content requires subscription.** The brain channel is not implicit — a session only sees brain entries if `brain` is in its `AGENTIHOOKS_BASE_CHANNELS` env var (see [Subscribing](#subscribing) above). The default profile ships `"brain,amygdala"` so brain content lands by default; remove `brain` from the env list (per-repo or per-container) to opt out.

### Source interface

The adapter uses a pluggable source interface:

```python
class BrainSource:
    def fetch(self) -> list[BrainEntry]: ...

class FileBrainSource(BrainSource):
    """Reads markdown files from a directory."""

# Future:
class McpBrainSource(BrainSource):
    """Fetches from an MCP tool or API."""
```

The foundation ships with `FileBrainSource`. It reads from a configurable directory — point it at your vault via NFS mount, symlink, or direct path. The interface is designed so swapping to an API-backed source later is a one-module change.

### Brain file format

Markdown files with YAML frontmatter in the brain directory:

```markdown
---
id: hot-arcs-2026-04-10
title: Active Hot Arcs
priority: 10
ttl: 3600
severity: info
---

## Currently Active

| Arc | Heat | Status |
|-----|------|--------|
| LiteLLM MCP Overhaul | 9.2 | shipping |
| Brain MVP | 8.7 | block-1 |
| Overlay System | 7.5 | shipped |
```

| Field | Required | Default | Purpose |
|-------|----------|---------|---------|
| `id` | No | filename stem | Dedup key |
| `title` | No | filename stem | Display name in injection |
| `priority` | No | 5 | Sort order (higher = injected first) |
| `ttl` | No | 3600 | Seconds until the message expires |
| `severity` | No | info | Broadcast severity tier |

### Refresh mechanism

Brain content changes slowly — you don't need to re-read the filesystem every turn. The adapter uses a **turn counter** (same pattern as context refresh):

1. Counter increments on every `UserPromptSubmit`
2. Every `BRAIN_REFRESH_INTERVAL` turns (default: 30), the adapter re-reads the source
3. If the content hash changed → clears old brain messages, publishes new ones
4. If unchanged → no-op (existing messages keep being delivered by the broadcast system)

On `SessionStart`, the adapter does an immediate one-shot publish so fresh sessions get brain content on their first turn.

### Configuration

```bash
# ~/.agentihooks/.env
BRAIN_ENABLED=true                      # master switch (default: false)
BRAIN_SOURCE_TYPE=file                  # "file" (future: "mcp")
BRAIN_SOURCE_PATH=~/.agentihooks/brain  # directory to read from
BRAIN_CHANNEL=brain                     # which broadcast channel to publish to
BRAIN_REFRESH_INTERVAL=30               # re-read source every N turns
```

### MCP tools

| Tool | Purpose |
|------|---------|
| `brain_refresh()` | Force re-read source and republish now |
| `brain_status()` | Return source type, path, entry count, content hash, refresh interval |

---

## Putting it together: the nervous system

The channel system and brain adapter are the plumbing layer for the Anton Brain MVP. Here's how the full system flows:

```
Obsidian Vault (TurboVault)
    ↓ NFS mount / symlink
~/.agentihooks/brain/
    ↓ FileBrainSource reads *.md
brain_adapter.py
    ↓ publishes to channel "brain"
broadcast.json
    ↓ UserPromptSubmit hook filters by subscription
Sessions with "channels": ["brain"]
    ↓ inject_context()
Agent sees hot arcs, active context, operational memory
```

The brain-keeper daemon (separate agent, runs on cron) maintains the brain directory — computing heat scores, promoting/demoting arcs, generating `_hot-arcs.md`. The brain adapter doesn't care who writes the files; it just reads and publishes.

The amygdala (emergency broadcast) publishes directly to a channel like `"amygdala"` at `critical` severity — every subscribed session gets the alarm on every tool call. Non-subscribed sessions are insulated.

### Example: antoncore as test bunny

To subscribe a single repo to the `brain` channel beyond the operator's default subscription:

```json
// antoncore/.claude/settings.local.json
{
  "env": {
    "AGENTIHOOKS_BASE_CHANNELS": "brain,amygdala,deploy"
  }
}
```

Any agent launched in the antoncore working directory inherits this list (Claude Code merges it on top of the user-global `~/.claude/settings.json`). Agents launched in repos without the override see only the profile-level default. The `settings.local.json` form is gitignored — use `settings.json` instead if the team should share the override.

---

## Design decisions

**Why channels on broadcast, not a separate system?**

The broadcast system already solves delivery: per-turn injection, severity tiers, TTL, expiry, dedup. Adding a `channel` field and a read-time filter gives us targeted delivery without duplicating infrastructure. One state file, one delivery path, one set of hooks.

**Why read-time filtering instead of write-time routing?**

Messages are written once, read many times. A session's subscriptions can change without touching the message store. No session-specific message copies to manage. The tradeoff is that every session scans every message — but with a 50-message cap and JSON parsing, this is sub-millisecond.

**Why a turn counter for brain refresh instead of file watchers?**

Hooks run in a subprocess that exits after each event. There's no long-lived process to run `inotifywait`. The turn counter is the established pattern (context refresh, image persistence reminder) — it's simple, stateless across process boundaries, and good enough for content that changes on the order of minutes, not milliseconds.

**Why is the brain adapter disabled by default?**

Not every agentihooks user has a brain. The adapter adds filesystem reads on the hot path (every N turns). Opt-in via `BRAIN_ENABLED=true` keeps the default install lean.

---

*See also: [Fleet Command](fleet-command.md) for the global broadcast system that channels extend.*
