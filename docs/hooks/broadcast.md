---
title: Broadcast System
nav_order: 4
parent: Hook System
permalink: /docs/hooks/broadcast/
---

# Broadcast System
{: .no_toc }

Send a message. Every active Claude Code session sees it — on the next hook event, before the next tool call. No servers, no daemons, no pub/sub infrastructure. Just a shared file and a hook.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Overview

The Broadcast System is a real-time messaging layer that delivers operator messages to **all active Claude Code sessions simultaneously**. It exploits the fact that Claude Code hooks are stateless subprocesses — every session, on every event, spawns a fresh process that reads shared state. A shared broadcast file becomes a PA system for all running AI agents.

### The problem

When you have multiple Claude Code sessions open (or thousands of agents in Kubernetes), there is no way to tell all of them something at once. If production goes down, if credentials are rotating, if a deploy freeze is in effect — each agent operates in isolation, unaware of what the operator needs them to know.

### The solution

A shared file (`~/.agentihooks/broadcast.json`) that any process can write to and every hook invocation reads. Messages are injected into each session's context window as banners, ensuring every agent sees the broadcast on its next event.

```
OPERATOR                           SHARED STATE                    ALL ACTIVE SESSIONS
                                                                   
agentihooks broadcast              ~/.agentihooks/                 Session 1 ──▶ sees banner
  "deploy freeze"            ──▶   broadcast.json    ◀── read ──  Session 2 ──▶ sees banner
                                                      ◀── read ──  Session 3 ──▶ sees banner
                                                      ◀── read ──  ...
                                                      ◀── read ──  Session N ──▶ sees banner
```

---

## Quick Start

```bash
# Tell every active session to stop deploying — right now
agentihooks broadcast -s critical "Production incident — read-only mode. Do NOT deploy."

# Declare a deploy freeze for the night
agentihooks broadcast -s alert -t 8h "Deploy freeze until 06:00. No pushes to any branch."

# Natural language — let Haiku figure out the right severity and TTL
agentihooks broadcast emit "we have a prod incident, all agents should halt any deployments"

# Check what's active
agentihooks broadcast --list

# Lift it early
agentihooks broadcast --clear
```

---

## Use Cases

### Deploy Freeze

It's 11pm. You're freezing deploys until after a maintenance window.

```bash
agentihooks broadcast -s alert -t 8h \
  "Deploy freeze in effect until 06:00 UTC. Do NOT push, deploy, or restart any service."
```

Every agent — whether working on frontend, backend, or infra — sees the banner on their next turn. Alert severity means it re-injects every turn until the TTL expires. You don't have to remember to cancel it.

---

### Production Incident

The database is degraded. You need every agent to stop making writes immediately.

```bash
agentihooks broadcast -s critical \
  "INCIDENT: DB-PROD is degraded. Read-only mode — do NOT execute writes, migrations, or deploys."
```

Critical severity fires on every turn **and** before every tool call. An agent mid-task, about to run `kubectl apply`, sees the message before the tool executes. It cannot miss it.

When the incident is resolved:

```bash
agentihooks broadcast --clear
```

---

### Credential Rotation

Credentials are being rotated. Agents using the old values need to know.

```bash
agentihooks broadcast -s critical -t 30m \
  "Credential rotation in progress. GITHUB_TOKEN and REGISTRY_TOKEN are being cycled. Pause any tasks requiring those credentials."
```

The 30-minute TTL matches the rotation window. After that, the message self-cleans.

---

### Maintenance Window

You're upgrading the Kubernetes cluster. Agents should not schedule new workloads.

```bash
agentihooks broadcast -s alert -t 2h \
  "K8s cluster maintenance 02:00–04:00 UTC. Do not apply manifests or trigger rollouts."
```

---

### Team Coordination

Multiple engineers, multiple sessions, one shared instruction.

```bash
agentihooks broadcast -s info \
  "QA environment is reserved for release testing until 17:00. Use staging-dev for feature work."
```

Info severity delivers once per session — it's a heads-up, not a repeated warning. Each session sees it exactly once.

---

### AI-Assisted Broadcast

Not sure what severity to pick? Use the `emit` subcommand. It sends your natural language to Claude Haiku, which constructs the right broadcast and fires it.

```bash
agentihooks broadcast emit "sonarqube is down, agents should skip code quality scans for now"
# → severity: info, ttl: 4h, message: "SonarQube is currently unavailable. Skip code quality scan steps."

agentihooks broadcast emit "prod is down, stop everything"
# → severity: critical, ttl: 30m, message: "Production incident in progress. Halt all deploys and destructive operations."
```

`emit` runs Haiku in a sandboxed subprocess with `Bash(agentihooks*)` as the only permitted tool — it can only call back into the agentihooks CLI. No rogue actions, no context leakage.

---

## Severity Levels

| Severity | When agents see it | Persistent | Default TTL | Use for |
|----------|--------------------|------------|-------------|---------|
| `critical` | Every turn + every tool call | Yes | 30 min | Production incidents, credential rotation, emergency stops |
| `alert` | Every turn | Yes | 1 hour | Deploy freezes, maintenance windows, policy changes |
| `info` | Once per session | No | 4 hours | FYI notices, status updates, reminders |

**Critical** is the most aggressive. The agent sees the message before every user turn AND before every tool call. An agent making 5 tool calls per turn sees the critical message 6 times. It cannot miss it.

---

## CLI Reference

### `agentihooks broadcast`

```
agentihooks broadcast [OPTIONS] MESSAGE
```

| Flag | Default | Description |
|------|---------|-------------|
| `-s`, `--severity` | `alert` | Severity level: `critical`, `alert`, `info` |
| `-t`, `--ttl` | (per severity) | Time-to-live: `30m`, `1h`, `4h`, `24h`, or integer seconds |
| `--persistent` | (per severity) | Force persistent re-injection on every applicable event |
| `--source` | `operator` | Source tag: `operator`, `system`, `cron`, `api` |
| `--list` | | Show all active broadcasts with IDs and expiry times |
| `--clear` | | Clear all active broadcasts |
| `--clear ID` | | Clear a specific broadcast by ID |

### `agentihooks broadcast emit`

```
agentihooks broadcast emit "NATURAL LANGUAGE DESCRIPTION"
```

Sends the description to Claude Haiku, which determines the right severity, TTL, and message text, then fires the broadcast. Haiku runs sandboxed with `Bash(agentihooks*)` only.

```bash
# Examples
agentihooks broadcast emit "deploy freeze tonight, no pushes"
agentihooks broadcast emit "prod incident, all agents stop"
agentihooks broadcast emit "sonarqube is back up"
```

---

## Architecture

### Broadcast File

Location: `~/.agentihooks/broadcast.json`

```json
{
  "_version": 1,
  "messages": [
    {
      "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
      "message": "Deploy freeze until 3am — do NOT push or deploy to any environment.",
      "severity": "alert",
      "persistent": true,
      "source": "operator",
      "created_at": "2026-04-07T22:00:00Z",
      "ttl_seconds": 18000,
      "expires_at": "2026-04-08T03:00:00Z",
      "delivered_to": ["sess-abc123", "sess-def456"]
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string (UUID) | Unique message identifier |
| `message` | string | The broadcast content seen by each agent |
| `severity` | string | `critical`, `alert`, or `info` |
| `persistent` | boolean | If true, re-inject every applicable event until TTL. If false, inject once per session. |
| `source` | string | Who sent it: `operator`, `system`, `cron`, `api` |
| `created_at` | ISO 8601 | When the message was created |
| `ttl_seconds` | integer | Time-to-live in seconds |
| `expires_at` | ISO 8601 | Computed expiry timestamp |
| `delivered_to` | array | Session IDs that have received this message (one-shot delivery tracking) |

### Active Session Registry

Location: `~/.agentihooks/active-sessions.json`

```json
{
  "sessions": {
    "session-abc123": {
      "started_at": "2026-04-07T20:00:00Z",
      "pid": 12345,
      "cwd": "/home/user/dev/my-project",
      "model": "claude-opus-4-6"
    }
  }
}
```

- `SessionStart` hook registers the session
- `SessionEnd` hook deregisters it
- Stale sessions (PID no longer alive) are cleaned up lazily on read

### Injection Mechanism

**UserPromptSubmit (stdout — system-reminder)**

For `alert` and `info` severity. Uses `inject_banner()` to print a formatted banner to stdout, which Claude Code captures and injects as a system-reminder.

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  BROADCAST ALERT                                                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Deploy freeze until 3am — do NOT push or deploy to any environment.         ║
║  Source: operator | Expires: 2026-04-08T03:00:00Z                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

**PreToolUse (additionalContext JSON)**

For `critical` severity only. Returns JSON that Claude Code appends to the tool's context — fires before EVERY tool call, meaning the agent sees the critical message mid-reasoning, not just at turn start.

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "BROADCAST CRITICAL: Deploy freeze — do NOT push or deploy. Expires: 2026-04-08T03:00Z"
  }
}
```

**SessionStart (stdout)**

Delivers any pending broadcasts to new or resuming sessions immediately on startup.

### Concurrency Safety

Multiple sessions may read and write `broadcast.json` simultaneously. Safety is ensured via:

1. **Atomic writes**: Write to `.tmp` file, then `os.replace()` (atomic on POSIX)
2. **File locking**: `fcntl.flock()` for delivery tracking updates (read-modify-write cycle)
3. **Redis primary**: When Redis is available, broadcasts are stored as a Redis hash with per-message TTL. File is the fallback.

### Lazy Cleanup

No dedicated cleanup process. Every hook invocation that reads `broadcast.json`:
1. Filters out expired messages (current time > `expires_at`)
2. Rewrites the file if any messages were removed
3. Expired broadcasts are cleaned up within seconds of expiry — the next hook event triggers it

---

## Hook Integration

### Where broadcast checks run

| Hook Event | What happens | Injection method |
|------------|-------------|-----------------|
| `SessionStart` | Register session. Deliver pending one-shot broadcasts. | stdout (inject_banner) |
| `UserPromptSubmit` | Check for undelivered messages. Inject all applicable. | stdout (inject_banner) |
| `PreToolUse` | Check for critical+persistent broadcasts only. | additionalContext JSON |
| `SessionEnd` | Deregister session from active-sessions.json. | N/A |

### Flow diagram

```
UserPromptSubmit fires
  ├── broadcast.py: read_broadcasts(session_id)
  │     ├── load broadcast.json
  │     ├── filter: not expired, not delivered to this session (or persistent)
  │     ├── for each message:
  │     │     ├── inject_banner(severity_title, message + metadata)
  │     │     └── mark as delivered (if one-shot)
  │     └── lazy cleanup: remove expired, rewrite file
  └── (existing hooks continue: secrets scan, context refresh, etc.)

PreToolUse fires
  ├── broadcast.py: get_critical_context(session_id)
  │     ├── load broadcast.json
  │     ├── filter: severity=critical AND (persistent OR not delivered)
  │     └── return additionalContext string (or None)
  └── hook_manager: if context returned, include in hookSpecificOutput JSON
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BROADCAST_ENABLED` | `true` | Enable/disable the broadcast system entirely |
| `BROADCAST_FILE` | `~/.agentihooks/broadcast.json` | Path to the broadcast file. Point to a shared mount for multi-node. |
| `BROADCAST_MAX_MESSAGES` | `50` | Maximum concurrent active broadcasts (oldest expire first at the limit) |
| `BROADCAST_CRITICAL_ON_PRETOOL` | `true` | Inject critical broadcasts on PreToolUse via additionalContext |

---

## Scale: From Laptop to Kubernetes

### Single machine (1–20 sessions)

File-based. `~/.agentihooks/broadcast.json` on local disk. All sessions read the same file. Atomic writes + flock for safety. Zero configuration needed.

### Shared filesystem (20–200 sessions)

NFS or EFS mount. Point `BROADCAST_FILE` to a shared path. All pods and nodes read the same file. Works with the same file-based implementation — NFS supports flock on most configurations.

```bash
BROADCAST_FILE=/mnt/shared/agentihooks/broadcast.json
```

### Redis (200+ sessions)

When `REDIS_URL` is set, broadcasts are stored as a Redis hash with per-message TTL. No file I/O, no locking, no cleanup needed — Redis handles expiry natively. The file path becomes a fallback for Redis downtime.

```
Redis key: agenticore:broadcast:messages
  └── {message_id}: JSON blob (with Redis TTL = broadcast TTL)

Redis key: agenticore:broadcast:delivered:{session_id}
  └── Set of message IDs already delivered
```

### Kubernetes operator pattern

In a K8s environment with hundreds of agent pods:

1. Operator sends broadcast via `agentihooks broadcast` (or API endpoint)
2. Message lands in Redis (shared across all pods)
3. Every agent pod's hook processes read from the same Redis
4. Delivery tracking per session ensures each agent sees it exactly once (or on every turn for persistent)
5. `agentihooks broadcast --list` shows delivery status across all sessions

---

## Active Session Tracking

### Registration

On `SessionStart`, the hook registers the session:

```python
{
  "session_id": payload["session_id"],
  "started_at": now_iso(),
  "pid": os.getpid(),  # parent Claude Code process
  "cwd": payload.get("cwd", ""),
  "model": payload.get("model", ""),
  "source": payload.get("source", "startup"),
}
```

### Deregistration

On `SessionEnd`, the hook removes the session entry. Stale sessions (PID no longer alive) are removed lazily on the next read — this handles crashed sessions that never fired `SessionEnd`.

### Visibility

```bash
agentihooks status
```

```
[OK] Active sessions: 4
     + sess-abc123 (PID 12345) ~/dev/my-project [claude-opus-4-6] 2h ago
     + sess-def456 (PID 23456) ~/dev/infra [claude-sonnet-4-6] 15m ago
     + sess-ghi789 (PID 34567) ~/dev/frontend [claude-haiku-4-5] 3m ago
     + sess-jkl012 (PID 45678) ~/dev/api [claude-opus-4-6] 1m ago
```

---

## Limitations

- **Pull, not push**: Broadcasts are pulled on hook events. An idle session (no user input, no tool calls) won't see a broadcast until the next event fires. Worst-case latency = time until the user's next message.
- **10,000 char cap**: Claude Code limits hook stdout and additionalContext to 10,000 characters per event. Broadcasts must be concise.
- **NFS flock**: Some NFS configurations don't support `flock()`. Use Redis in those environments.
- **No selective targeting**: Broadcasts go to ALL sessions. There is no per-session or per-project targeting — the PA system is for fleet-wide announcements by design.

---

## Future Scope

- **Selective targeting**: Broadcast to sessions matching a filter (project path, model, profile)
- **Acknowledgment**: Agents can acknowledge a broadcast, removing it from their queue
- **Escalation**: If a critical broadcast is not acknowledged within N minutes, escalate (email, Slack, kill session)
- **API endpoint**: HTTP endpoint for external systems to send broadcasts (CI/CD, monitoring, incident management)
- **Dashboard**: Web UI showing active sessions, pending broadcasts, delivery status
