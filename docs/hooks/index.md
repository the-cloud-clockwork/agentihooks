---
title: Hook System
nav_order: 4
has_children: true
---

# Hook System

AgentiHooks handles lifecycle events from **Claude Code and Codex**. Each harness runs a configured command with a JSON payload on stdin. AgentiHooks normalizes and dispatches it to shared handlers. The examples below use Claude Code settings; see the [Codex reference](../reference/CODEX-COMPAT.md) for `hooks.json`, permission and context-delivery differences.

## How it works

```mermaid
flowchart LR
    CC[Claude Code or Codex] -->|"hook_event_name + payload\n(via stdin)"| HM["python -m hooks"]
    HM -->|dispatch| H1[SessionStart handler]
    HM -->|dispatch| H2[PreToolUse handler]
    HM -->|dispatch| H3[Stop handler]
    H1 --> A1[Inject context]
    H2 --> A2[Scan secrets\nInject tool memory]
    H3 --> A3[Send error email\nAuto-save memory]
```

1. Claude Code fires a hook event and runs the configured shell command
2. `python -m hooks` reads the JSON payload from stdin
3. `hook_manager.py` reads `hook_event_name` and dispatches to the matching handler
4. The handler performs its actions (logging, context injection, secret scanning, etc.)
5. Exit code controls Claude Code's behavior: `0` = allow, `2` = block

## Configuration

Hooks are wired in `~/.claude/settings.json` under the `hooks` key. Each entry maps an event name to a shell command array:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 -m hooks"
          }
        ]
      }
    ]
  }
}
```

The install script (`agentihooks init`) writes this configuration automatically.

## Hook array behavior

{: .important }
Hook arrays are **replaced, not merged**, across scopes. All hooks must live in `~/.claude/settings.json` -- project-level settings cannot append to user-level hooks.

## Pages in this section

| Page | What it covers |
|------|---------------|
| [Events](events.md) | All 10 hook events with payload schemas and handler behavior |
| [Hook Injection Budget](injection-budget.md) | Claude event limits, context priority, shortening notices and tool memory configuration |
| [Context Preprocessor](context-preprocessor.md) | Token compression for injected banners and tool output (scope=all) |
| [MCP Transport](mcp-transport.md) | Running `agentihooks` over `sse`/`streamable-http` instead of stdio, for clients that filter stdio MCP servers out. Covers the daemon `agentihooks init` now starts, `agentihooks mcp status` for diagnosing config-vs-reality drift, the systemd and pidfile supervisors, and the explicit `session_id` the network mode requires. |
| [**Broadcast System**](broadcast.md) | **Real-time fleet messaging** — send messages to Claude Code and Codex sessions sharing the broadcast store. Deploy freezes, incident response, credential rotation, team coordination. Optional channel filtering via `AGENTIHOOKS_BASE_CHANNELS` env var for targeted delivery. |

{: .highlight }
**Broadcast System** is one of AgentiHooks' most powerful features. One command reaches every active session — no servers, no daemons, no pub/sub infrastructure required. [Learn more →](broadcast.md)
