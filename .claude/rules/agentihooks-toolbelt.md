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
| `channel_publish` | You need to **coordinate** with other live agents — "restarting litellm-0, hold off." Coordination only; knowledge goes to brain markers / `brain_ingest`, never here. |
| `channel_list` | You want to see which channels carry active messages before publishing or acting. |
| `channel_acknowledge` | A persistent broadcast has been handled and you want it to stop re-injecting **for this session** (leaves it live for others). The ID is in the banner as `ID: <id>`. |
| `channel_clear` | A broadcast is stale fleet-wide and should be removed — by `message_id`, by `channel`, or all. |
| `brain_status` | You want the brain adapter's current state: source, entry count, channel. Diagnostic first stop when brain context looks stale or missing. |
| `brain_refresh` | You've changed brain source content and need it republished **now** rather than on the next counter-gated tick. |
| `enforcement_set` | A discipline must survive context drift — register a drumbeat that re-injects every N tool calls (global, permanent until cleared). Cheap: context tokens only, no API call. |
| `enforcement_list` | Check which drumbeats are active before adding or clearing one. |
| `enforcement_clear` | A drumbeat's job is done — clear by `enforcement_id`, by `tag`, or all. |

Subscriptions are **not** a tool — they're operator-configured via
`AGENTIHOOKS_BASE_CHANNELS`. You publish and consume; the operator decides who
listens.
