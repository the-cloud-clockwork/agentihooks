# AgentiHooks — What You're Running Inside, and Its Toolbelt

Your session is wrapped by **AgentiHooks**: a lifecycle-hook layer that guards,
compresses, and coordinates Claude Code, Codex, and Copilot CLI sessions in the
fleet. It ships MCP tools under the **`hooks-utils`** server. Use them when the
situation below fires. Profiles and host capabilities differ; an unavailable
tool or hook path is unavailable in this session.

## What it does (four pillars)

- **Guardrails** — secret blocking, retry circuit breaker, branch/PR gating,
  production lockdown, mutation guards, and CI-manifesto signal parsing across
  the lifecycle events each host supports.
- **Context Intelligence** — token compression, brain injection, session-start
  enforcement loading, immediate first delivery, cadence, rule refresh, and tool
  memory.
- **Fleet Command** — file-based broadcast pub/sub with channel targeting, bridged
  to the brain.
- **Identity** — the profile/bundle merge that installed these very rules.

## Injected context is operative

`ENFORCEMENT` and `BROADCAST` blocks are live context, not status decoration.
Read each block completely before the next action and apply it to the current
plan, commands, delegation, and final result.

- **Enforcements are operator guardrails.** Follow every applicable enforcement
  within the active instruction hierarchy. Do not merely quote, acknowledge, or
  summarize it. If two instructions conflict, obey the higher-precedence one and
  state the conflict instead of silently ignoring either.
- **Broadcasts are fleet coordination.** Act on actionable messages according to
  their source, channel, severity, and content. A critical or nuclear hazard is
  considered before any affected tool call. Brain-adapter messages explicitly
  labelled as recalled context are evidence, not new operator directives.
- **Acknowledgement means handled.** Do not call `channel_acknowledge` to silence
  an unhandled message. Acknowledge only after its requested action or condition
  is complete for this session. Clear fleet-wide messages only when they are
  stale or resolved for every consumer.
- **Hook blocks are boundaries.** When a guard blocks an action, follow the
  remediation in the block. Do not evade it through another shell, tool, MCP,
  agent, or mutation surface.

## The `hooks-utils` MCP tools — and when to reach for them

| Tool | Reach for it when |
|---|---|
| `channel_publish` | You need to **coordinate** with other live agents — see *Broadcasts* below. Coordination only; knowledge goes to brain markers / `brain_ingest`, never here. |
| `channel_list` | Before publishing or acting, to see which channels currently carry live messages and how many. |
| `channel_acknowledge` | A persistent broadcast has been handled and should stop re-injecting **for this session** (stays live for other agents). The ID is in the banner as `ID: <id>`. |
| `channel_clear` | A broadcast is stale fleet-wide — remove it by `message_id`, by `channel`, or all. |
| `brain_status` | Diagnose the brain adapter — source, entry count, channel. First stop when brain context looks stale or missing. |
| `brain_refresh` | You changed brain source content and need it republished **now** instead of on the next counter-gated tick. |
| `enforcement_set` | A discipline must survive context drift — create a global runtime enforcement with a tool-call cadence. The CLI additionally supports project-local enforcements with `--local`. |
| `enforcement_list` | Check active drumbeats before adding or clearing one. |
| `enforcement_clear` | A drumbeat's job is done — clear by `enforcement_id`, by `tag`, or all. |

## Enforcements — first delivery, then cadence

Every effective enforcement is loaded once at SessionStart. An enforcement
created after startup is injected once on the session's next supported prompt or
tool event, then returns to its normal every-N-tool-calls cadence. Codex recovers
context through PostToolUse where PreToolUse cannot carry it.

Resolution order is bundle → profile chain → global runtime → project-local;
later entries with the same ID win. The MCP tools manage the global runtime
store. Use `agentihooks enforcement ... --local` for
`<project>/.agentihooks/enforcements.json`.

## Your own session id — pass it to the tool that needs it

`channel_acknowledge` acts *as you*, so it needs to know which session you are.
SessionStart tells you the current host's session ID. Pass that exact value as
`session_id`.

Under the default stdio setup the server can infer it and the argument is
optional. Where `hooks-utils` runs as a shared network server, one process
serves every session and inference is impossible — an omitted argument returns
`no session id resolvable` rather than guessing, because guessing would mean
writing another agent's state. Pass it and both setups behave the same.

## Broadcasts — how a message reaches other agents

The fleet runs many agents at once. A broadcast is how one tells the others
something *now*, while they are mid-work. Two things decide who sees it and when:

**Channel = who is subscribed.** A message with no channel reaches every live
session. A channelled message reaches only sessions subscribed to that channel via
`AGENTIHOOKS_BASE_CHANNELS` (operator-set: profile env → repo settings → container
launch; default `brain,amygdala`). You publish and consume; the operator decides
who listens. `channel` is a free string — reuse an agreed name (`deploy-status`,
`ops-alerts`) so the intended peers, already subscribed, actually receive it.

**First delivery is immediate.** Every newly eligible broadcast is delivered
once on the session's next supported prompt or tool event. Codex uses
PostToolUse when PreToolUse cannot carry context. Later delivery depends on
persistence, throttling, and severity:

| Severity | Default behavior after first delivery | Use for |
|---|---|---|
| `nuclear` | Persistent, highest priority; recurring PreToolUse delivery only when that path is enabled | exposed credential or fleet-wide emergency |
| `critical` | Persistent; recurring PreToolUse delivery only when that path is enabled | hazard that must affect the next relevant action |
| `alert` / `warning` | Persistent prompt delivery, subject to deduplication and throttle | active condition or caution |
| `info` / `resolved` | One-shot by default | context or resolution notice |

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

Every subscribed peer receives that context on its next eligible event and
steers around you; persistent reminders continue subject to delivery throttling.
When the condition ends, retract it with `channel_clear` or publish a `resolved`
notice so the lane reopens.

## Other context features worth using

- **Tool memory** injects relevant prior failures before a tool call. Treat that
  history as evidence: change the next attempt when it identifies the same
  failure pattern.
- **Live rule refresh** is one-shot, not a cadence. After changing installed
  Claude rules, `agentihooks refresh-rules` sends the current rule set to sessions
  that were already running; new sessions load the current files at startup.
