---
title: Events
nav_order: 2
---

# Hook Events
{: .no_toc }

AgentiHooks registers handlers for all 11 Claude Code hook events.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Exit code semantics

| Exit code | Meaning |
|-----------|---------|
| `0` | Allow — Claude Code proceeds normally |
| `2` | Block — Claude Code cancels the action and injects the hook's stdout as a warning |

---

## SessionStart

**When:** A new Claude Code session begins.

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Unique session identifier |

**Handler actions:**

1. Creates `/tmp/<session_id>/` as the session working directory
2. Injects a context message into Claude's context window with session awareness
3. Logs output token limit awareness if `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is set
4. If `MCP_HYGIENE_ENABLED=true`: injects a reminder to disable unused MCP servers via `/mcp` to reduce per-turn token overhead

---

## SessionEnd

**When:** The session ends normally (not via Stop).

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier |
| `transcript_path` | string | Path to the session transcript JSONL file |

**Handler actions:**

1. Parses the transcript to extract metrics (`num_turns`, `duration_ms`)
2. Logs all transcript entries to the hooks log
3. If `FILE_READ_CACHE_ENABLED=true`: clears the file read cache for this session from Redis
4. Cleans up the `/tmp/<session_id>/` directory

---

## UserPromptSubmit

**When:** The user submits a prompt (before Claude processes it).

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier |
| `prompt` | string | The user's raw prompt text |

**Handler actions:**

1. Scans the prompt for secrets and credentials using regex patterns
2. If secrets are detected: injects a warning into the context (does **not** block — warnings only at this stage)

---

## PreToolUse

**When:** Before any tool executes. This is the primary security gate.

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier |
| `tool_name` | string | Name of the tool about to run |
| `tool_input` | object | Tool input parameters |
| `transcript_path` | string | Path to transcript |

**Handler actions:**

1. Logs the transcript entry
2. **Secret scanning** — scans `tool_input` for credentials; exits with code `2` (block) if found
3. **File read deduplication** — if `FILE_READ_CACHE_ENABLED=true` and `tool_name == "Read"`: checks whether the file was already read this session and is unmodified (by mtime). If so, exits with code `2` and tells Claude to use the content already in context
4. **Tool memory injection** — looks up past errors for this tool and injects them as context so the agent can avoid repeating mistakes

**Exit codes used:**

- `0` — tool is safe to run
- `2` — secret detected **or** redundant file read blocked; action blocked with explanation

---

## PostToolUse

**When:** After a tool completes (success or failure).

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier |
| `tool_name` | string | Name of the tool that ran |
| `transcript_path` | string | Path to transcript |
| `tool_output` | string | Tool's stdout |
| `tool_error` | string | Tool's stderr (empty on success) |

**Handler actions:**

1. Logs the transcript entry
2. If `BASH_FILTER_ENABLED=true` and `tool_name == "Bash"`: detects verbose output categories (docker logs, kubectl, git log, test runners, build tools) and truncates to configured limits before it accumulates in the context window. Filtered output is re-emitted via `additionalContext` so Claude still sees the relevant portion
3. If `FILE_READ_CACHE_ENABLED=true` and `tool_name == "Read"`: records the file path and its current mtime in the session cache (Redis or memory) so future re-reads can be detected
4. If `tool_error` is non-empty: records the error pattern to the tool memory file (`~/.agenticore_tool_memory.ndjson`) for future injection

---

## Stop

**When:** The agent stops (task complete or unrecoverable error). This is the most active handler.

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier |
| `transcript_path` | string | Path to transcript |

**Handler actions:**

1. Parses transcript to extract metrics (`num_turns`, `duration_ms`)
2. Scans transcript for MCP errors that `PostToolUse` may have missed
3. If errors found and email is configured (`SMTP_SERVER`): sends an error notification email
4. Logs all transcript entries
5. If `MEMORY_AUTO_SAVE=true`: saves a session digest to the memory store

---

## SubagentStop

**When:** A subagent (spawned agent) stops.

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Subagent's session identifier |
| `transcript_path` | string | Path to subagent transcript |

**Handler actions:**

1. Logs the subagent's transcript entries to the hooks log

---

## StatusLine

**When:** Claude Code polls for terminal status bar text (fires frequently — once per turn or more).

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier |
| `context_window` | object | `{used: int, remaining: int}` — current context window state |
| `model` | string | Active model identifier |

**Handler actions:**

1. If `TOKEN_MONITOR_ENABLED=true`: computes fill percentage, burn rate delta (vs previous turn stored in Redis), and outputs a status line to stdout. Claude Code renders this in the terminal status bar:
   ```
   ctx: 234K/1M (23%) | burn: 12K/turn | model: sonnet-4.6
   ```
2. If fill % crosses `TOKEN_WARN_PCT` (default 60%) for the first time this session: injects a warning banner into Claude's context
3. If fill % crosses `TOKEN_CRITICAL_PCT` (default 80%) for the first time this session: injects a critical banner

Threshold warnings are **edge-triggered**: each level fires at most once per session (tracked in Redis at `agenticore:token_warn:{session_id}`). When Redis is unavailable, warnings fire every time the threshold is exceeded.

**Exit codes used:**

- `0` always — never blocks

---

## Notification

**When:** Claude Code sends a notification (e.g., requesting user attention).

**Payload fields:** varies — the entire notification data object.

**Handler actions:**

1. Logs the notification event and payload

---

## PreCompact

**When:** Claude Code is about to compact the context window.

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier |

**Handler actions:**

1. Logs a pre-compaction event marker

---

## PermissionRequest

**When:** Claude Code requests permission for an action that requires user approval.

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | string | Tool requesting permission |
| _(other fields)_ | varies | Permission metadata |

**Handler actions:**

1. Logs the permission request and tool name

---

## Token Control Layer

`PreToolUse`, `PostToolUse`, `SessionStart`, `SessionEnd`, and `StatusLine` work together to reduce context window consumption:

```mermaid
sequenceDiagram
    participant Agent
    participant SessionStart
    participant StatusLine
    participant PreToolUse
    participant PostToolUse
    participant SessionEnd
    participant Redis

    SessionStart->>Agent: MCP hygiene reminder
    loop Each turn
        StatusLine->>Redis: read prev used tokens
        StatusLine->>Agent: emit "ctx: X/1M (23%)" to status bar
        StatusLine->>Agent: inject warning banner (first threshold crossing only)
        Agent->>PreToolUse: about to Read file.py
        PreToolUse->>Redis: was file.py read + unmodified?
        Redis-->>PreToolUse: yes → BlockAction (exit 2)
        PreToolUse-->>Agent: "already in context, use it"
        Agent->>PostToolUse: Bash docker logs ... output
        PostToolUse->>Agent: re-emit truncated output via additionalContext
        PostToolUse->>Redis: mark file.py as read (mtime stored)
    end
    SessionEnd->>Redis: delete file_cache + file_mtime keys
```

Configure via the [Token Control](configuration/#token-control) environment variables.

---

## Tool memory learning

`PreToolUse` and `PostToolUse` work together to implement cross-session error learning:

```mermaid
sequenceDiagram
    participant Agent
    participant PreToolUse
    participant ToolMemory as Tool Memory<br/>(.ndjson)
    participant PostToolUse

    Agent->>PreToolUse: about to call tool X
    PreToolUse->>ToolMemory: look up past errors for tool X
    ToolMemory-->>PreToolUse: last N error patterns
    PreToolUse-->>Agent: inject error patterns as context
    Agent->>PostToolUse: tool X returned error Y
    PostToolUse->>ToolMemory: record error Y for tool X
```

Configure via:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_TOOL_MEMORY_PATH` | `~/.agenticore_tool_memory.ndjson` | Memory file path |
| `AGENTICORE_TOOL_MEMORY_MAX` | `100` | Maximum stored entries |
| `AGENTICORE_TOOL_MEMORY_SHOW` | `15` | Entries injected per PreToolUse |
