# AgentiHooks — What You're Running Inside, and Its Toolbelt

Your session is wrapped by **AgentiHooks**: a lifecycle-hook layer that guards,
compresses, and coordinates every Claude Code session in the fleet. It ships its
own MCP tools under the **`hooks-utils`** server. They are yours to use — reach
for them when the situation below fires. Not every profile enables every tool; if
one isn't in your tool list, its category is off for this profile.

## What it does (four pillars)

- **Guardrails** — two-tier secret blocking, retry circuit breaker, branch/PR
  gating, prod lockdown, CI-manifesto signal parsing. Mostly invisible; it blocks
  or warns at PreToolUse.
- **Context Intelligence** — token compression, brain injection, one-shot rule
  refresh, tool memory.
- **Fleet Command** — file-based broadcast pub/sub with channel targeting, bridged
  to the brain.
- **Identity** — the profile/bundle merge that installed these very rules.

## The `hooks-utils` MCP tools — and when to reach for them

| Tool | Reach for it when |
|---|---|
| `channel_publish` | You need to **coordinate** with other live agents — see *Broadcasts* below. Coordination only; knowledge goes to brain markers / `brain_ingest`, never here. |
| `channel_list` | Before publishing or acting, to see which channels currently carry live messages and how many. |
| `channel_acknowledge` | A persistent broadcast has been handled and should stop re-injecting **for this session** (stays live for other agents). The ID is in the banner as `ID: <id>`. |
| `channel_clear` | A broadcast is stale fleet-wide — remove it by `message_id`, by `channel`, or all. |
| `brain_status` | Diagnose the brain adapter — source, entry count, channel. First stop when brain context looks stale or missing. |
| `brain_refresh` | You changed brain source content and need it republished **now** instead of on the next counter-gated tick. |
| `enforcement_set` | A discipline must survive context drift — a drumbeat that re-injects every N tool calls (global, permanent until cleared). Cheap: context tokens only, no API call. |
| `enforcement_list` | Check active drumbeats before adding or clearing one. |
| `enforcement_clear` | A drumbeat's job is done — clear by `enforcement_id`, by `tag`, or all. |

## Broadcasts — how a message reaches other agents

The fleet runs many agents at once. A broadcast is how one tells the others
something *now*, while they are mid-work. Two things decide who sees it and when:

**Channel = who is subscribed.** A message with no channel reaches every live
session. A channelled message reaches only sessions subscribed to that channel via
`AGENTIHOOKS_BASE_CHANNELS` (operator-set: profile env → repo settings → container
launch; default `brain,amygdala`). You publish and consume; the operator decides
who listens. `channel` is a free string — reuse an agreed name (`deploy-status`,
`ops-alerts`) so the intended peers, already subscribed, actually receive it.

**Severity = when a peer sees it, and how insistently.** Same message, different
reach:

| Severity | A subscribed peer sees it… | Use for |
|---|---|---|
| `info` | **once**, on their next turn | FYI that doesn't need to interrupt |
| `alert` | on **every turn**, until it expires or they `channel_acknowledge` it | a condition they must keep in mind while working |
| `critical` | every turn **and before every tool call** (injected into PreToolUse) | a hazard that must stop the wrong action *before* it happens |
| `nuclear` | same as critical, top priority | a credential/secret exposed anywhere |

TTL defaults track severity (critical/nuclear ~30 min, alert ~1 h, info ~4 h), so
transient coordination expires on its own; pass `ttl_seconds` to override.

**The worked case — concurrent agents on one pipeline.** You discover another agent
is already working the same lane you're about to touch (same service, same deploy,
same files) and your changes would collide with or overwrite theirs. Don't race —
publish first:

```
channel_publish(
  channel="deploy-status",
  message="agent-A is mid-rollout on litellm dev — holding the :dev tag. "
          "Do not push litellm or restart the pod until I clear this.",
  severity="alert",
)
```

Every subscribed peer now carries that context on every turn and steers around
you. When you're done, retract it with `channel_clear` (or downgrade to a resolved
`info`) so the lane reopens. This is the whole point of Fleet Command: agents that
would otherwise clobber each other coordinate through the channel instead.
