---
title: Observability with Langfuse
parent: Reference
nav_order: 8
---

# Observability with OpenTelemetry and Langfuse

AgentiHooks sends telemetry along five paths. Only the agent trace exporter
writes to Langfuse; the others speak OTLP to an OpenTelemetry collector.

| Path | What it sends | Where it goes | Turned on by |
|---|---|---|---|
| Agent trace exporter | One trace per Claude or Codex session: turns, model calls, tool calls, with prompt, reply and tool text | Langfuse, OTLP/HTTP traces | `AGENTIHOOKS_LANGFUSE_ENABLED` plus a Langfuse key pair |
| Hook events and gauges | Log events (`agentihooks.*`) and gauges such as `agentihooks.tokens.fill_pct` from hook handlers; spans such as `agentihooks.session.stop` | `OTEL_EXPORTER_OTLP_ENDPOINT` (collector), gRPC or HTTP | `OTEL_HOOKS_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `CLAUDE_CODE_ENABLE_TELEMETRY` |
| Brain spans | `brain.inject`, `brain.delivery`, `brain.marker_write` | `OTEL_EXPORTER_OTLP_ENDPOINT` `/v1/traces` (collector) | `OTEL_HOOKS_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` |
| Claude Code native telemetry | Claude Code's own metrics and log events | `OTEL_EXPORTER_OTLP_ENDPOINT` (collector) | `CLAUDE_CODE_ENABLE_TELEMETRY=1` and the `OTEL_*` exporter variables in Claude Code's environment: a settings `env` block from the bundle (the anton profile sets the endpoint there), or `agentihooks init-agent` with `AGENTIHOOKS_OTEL_COLLECTOR` set |
| Codex native telemetry | Codex log events | `AGENTIHOOKS_OTEL_COLLECTOR` `/v1/logs` | `agentihooks init-agent --agent codex` with `AGENTIHOOKS_OTEL_COLLECTOR` set |

Langfuse accepts OTLP traces only, so metrics and log events never reach it.
Codex sessions also reach Langfuse through the agent trace exporter.

Claude Code removes every `OTEL_*` variable from the environment of hook
processes. A hook therefore never sees `OTEL_EXPORTER_OTLP_ENDPOINT` from the
Claude settings `env`; hook events, gauges and brain spans from a Claude session
stay off unless that variable comes from an AgentiHooks env file
(`~/.agentihooks/.env` or `~/.agentihooks/*.env`). The Langfuse switch carries the
`AGENTIHOOKS_` prefix for this reason.

## The agent trace exporter

Each session has one background exporter (`hooks/observability/trace_flush.py`),
started by the first SessionStart, UserPromptSubmit, PreCompact, Stop or
SessionEnd hook that finds none alive. Those hooks only record a flush request
and return; none of them waits on the network. The exporter holds an exclusive
lock for its whole life, so a session never has two.

- **While the agent works.** It wakes every
  `AGENTIHOOKS_TRACE_FLUSH_INTERVAL_SEC` (15) seconds, and at once on a hook's
  request, and exports when the transcript grew or earlier work is pending. A
  wake makes at most `AGENTIHOOKS_TRACE_FLUSH_ATTEMPTS` (3) attempts, each a
  child running `export_session` (`hooks/observability/agent_trace.py`) killed
  after `AGENTIHOOKS_TRACE_FLUSH_ATTEMPT_TIMEOUT_SEC` (5) seconds. An open turn
  is exported as it stands; a tool call without its result yet carries
  `tool.outcome.state=missing` and is updated on a later wake.
- **When the agent process ends** (Stop and exit, retirement, a kill mid turn)
  the exporter drains once more with the same budget and exits. A resume of the
  same session under a new process hands it the running exporter instead of
  starting a second one.
- **Freshness fields.** The root carries `agentihooks.export.trigger`
  (`request:<hook>`, `interval` or `final`) and
  `agentihooks.export.unwritten_events.state=unavailable`: events the harness
  has not yet written to its transcript cannot be exported.
- **One trace per session.** The trace id is derived from the session id, so
  every export of a session lands in the same trace. A cursor in
  `~/.agentihooks/agent_trace/<session id>.json` records what the backend
  accepted; each export sends only new or changed observations.
- **Long sessions.** The cursor holds at most 8 MB of unsent records and
  observations. Records past that wait for the next export, so a session of
  any length keeps exporting; while a backlog waits the exporter logs
  `agent trace export overflow` and counts an `overflow` traces signal.
  Accepted records whose
  observations can no longer change leave the cursor, and the next read starts
  after them.
- **Span tree.** Root `agent` observation named after the agent
  (`AGENTIHOOKS_AGENT_NAME`, else `agent-session`); a `turn N` span per user
  prompt; a `generation` per model message with token usage; a `tool` span per
  tool call with its input, output and error flag.
- **Full text, masked.** Prompts, replies and tool input and output are
  exported after strict secret redaction (`hooks.secrets.redact`). Each field is
  capped at `AGENTIHOOKS_LANGFUSE_FIELD_MAX_CHARS` (32000) characters.
  Claude keeps only a preview of a large tool output in its transcript and
  writes the whole output to a file in the session's `tool-results` folder; the
  exporter sends that file through the same masking and cap. When the file
  cannot be read, the preview is sent and the characters it leaves out count as
  truncation.
- **Transfers.** A session started by a quota handoff (`init-agent --handoff`)
  or a swarm seat handoff carries `agentihooks.correlation.predecessor.session.id`
  and `predecessor.trace.id` naming the session it continues; any other launch
  carries `predecessor.session.id.state=missing`.
- **Codex compaction.** A Codex `compacted` record keeps the session in one
  trace and is not exported as a turn. `codex exec resume <id> /compact` sends
  `/compact` as an ordinary prompt and writes no `compacted` record, so under
  `exec` only Codex's automatic compaction is covered.
- **Identity.** Session id, user id (the routed Claude account, else `$USER`)
  and tags `swarm:`, `agent:`, `lane:`, `task:`, `account:`, each only when its
  value is set. Swarm spawns carry all five; a session opened by hand carries
  `account:` when it is routed.
- **Codex.** A Codex transcript (`session_meta` records) is normalised into the
  same entries, so Codex sessions get the same trace shape.
- **Failures.** A failed export writes
  `agent_trace export failed endpoint=… status=… reason=…` to
  `~/.agentihooks/logs/async-hooks.log` and leaves the work pending in the
  cursor, so the next wake retries it. An attempt past its limit logs
  `trace_flush <session id>: attempt timed out after 5.0s`.

## Settings

### Turn Langfuse on for a profile

In the profile's `profile.yml`:

```yaml
otel:
  enabled: true
  langfuse:
    enabled: true
```

`agentihooks init` resolves the profile chain, then the settings profile, and
writes `AGENTIHOOKS_LANGFUSE_ENABLED=1` (or `0`) into:

- the Claude settings `env` block;
- the Codex hook wrapper, as a default a value already in the environment
  overrides.

A later profile in the chain overrides an earlier one. `otel.enabled: false`
writes `0` whatever `langfuse.enabled` says. A profile with neither key writes
nothing, and tracing stays off. The anton profile turns it on.
`agentihooks init-agent` exports the switch for the sessions it launches: the
caller's value when set, else the installed profile's; a swarm spawn with neither
gets `1`.

Run `agentihooks init` after changing `profile.yml`. Sessions already running
keep the old value.

The `endpoint`, `public_key` and `secret_key` fields under `otel.langfuse` in
`profile.yml` are not read. Keys and endpoint come from the environment.

### Keys and endpoint

Put these in `~/.agentihooks/.env` or a companion `~/.agentihooks/*.env`
file. The hook loads them at start; a value already in the process environment
wins.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTIHOOKS_LANGFUSE_ENABLED` | off | The switch. `OTEL_LANGFUSE_ENABLED` is read too, but Claude Code hides it from hooks. |
| `OTEL_LANGFUSE_PUBLIC_KEY`, `OTEL_LANGFUSE_SECRET_KEY` | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Project key pair. The project the keys belong to is the project traces land in. Both must be set or nothing is exported. |
| `OTEL_LANGFUSE_ENDPOINT` | `http://10.10.30.200/api/public/otel` | OTLP base URL. The exporter posts to `<endpoint>/v1/traces`. |
| `OTEL_LANGFUSE_HOST_HEADER` | `langfuse.homeofanton.com` when the endpoint is the default, else none | `Host` header for an ingress that routes by name. |
| `AGENTIHOOKS_LANGFUSE_FIELD_MAX_CHARS` | `32000` | Cap per exported text field. |

On the Anton workbench the keys in the env files belong to the Langfuse project
`agent-swarm`.

### Collector paths

Hook events, gauges and brain spans read the standard variables:

| Variable | Default | Meaning |
|---|---|---|
| `OTEL_HOOKS_ENABLED` | `true` | Master switch for hook events, gauges and brain spans |
| `OTEL_HOOKS_SERVICE_NAME` | `agentihooks` | `service.name` on every AgentiHooks span, including agent traces |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | none | Collector base URL; nothing is sent without it |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` for events and gauges, `http/protobuf` for brain spans | Wire protocol |
| `OTEL_HOOK_LOG_FANOUT` | `true` | Also send hook log lines to `<endpoint>/v1/logs` (HTTP only) |
| `AGENTIHOOKS_OTEL_COLLECTOR` | none | Collector `init-agent` hands to the Claude and Codex sessions it launches |

## Pointing the exporter at another OTLP backend

The exporter is a plain OTLP/HTTP protobuf trace exporter. It sends:

- `POST <OTEL_LANGFUSE_ENDPOINT>/v1/traces`;
- `Authorization: Basic base64(<public key>:<secret key>)`;
- `x-langfuse-ingestion-version: 4`;
- `Host: <OTEL_LANGFUSE_HOST_HEADER>` when set.

Any OTLP trace receiver that accepts or ignores Basic auth takes these spans.
Span attributes follow the OpenTelemetry `gen_ai.*` conventions; the
`langfuse.*` attributes are ignored by other backends.

Langfuse Cloud, for example:

```bash
# ~/.agentihooks/langfuse.env
AGENTIHOOKS_LANGFUSE_ENABLED=1
OTEL_LANGFUSE_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_LANGFUSE_PUBLIC_KEY=<project public key>
OTEL_LANGFUSE_SECRET_KEY=<project secret key>
```

Setting `OTEL_LANGFUSE_ENDPOINT` drops the default `Host` header. For a
self-hosted Langfuse behind a name-routing ingress, set
`OTEL_LANGFUSE_HOST_HEADER` as well. For a backend without auth, such as an
OpenTelemetry collector on `http://collector:4318`, set the endpoint to that base
URL and give the key variables any non-empty value: the exporter stays off while
either key is empty.

## Checking that a trace arrived

1. **Cursor.** After the session's next Stop,
   `~/.agentihooks/agent_trace/<session id>.json` exists and its `turns` count
   matches the session. It is written only after Langfuse accepted the spans.
2. **Failure log.** No new `agent_trace export failed` or
   `[async] agent_trace:` line in `~/.agentihooks/logs/async-hooks.log`.
3. **API.** Ask Langfuse for the session's trace:

   ```bash
   curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" -o trace.json "https://langfuse.homeofanton.com/api/public/traces?sessionId=<session id>"
   jq '.data[] | {name, tags}' trace.json
   ```

   One row, named after the agent, with its tags.
4. **UI.** In the project (`agent-swarm` on Anton), the Sessions view lists the
   session id; the trace shows the turn, generation and tool tree with text.
   Filter Traces by a tag such as `swarm:<slug>` to see one swarm.

The session id of a Claude session is printed at SessionStart. A swarm agent's
trace is also found by its `agent:<name>` tag.

## How the Doctor reads traces

The Doctor's `trace` detector (`scripts/doctor/traces_read.py`, active bindings in
`scripts/doctor/registry.py`, findings in `scripts/doctor/traces.py`) reads the
same Langfuse public API with `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
`LANGFUSE_HOST` (default `https://langfuse.homeofanton.com`). The swarm tick runs
it with the other detectors every `AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES` (10); it
is the one monitor of trace freshness. Among ticks, only `swarm tick`, the
command the timer unit runs, and the controller loop run it. A tick another swarm
command runs inline (`start`, `reopen`, `pause`, `stop`, `set`, `restore`, and so
`doctor start` and rig doctor) runs every other detector and leaves telemetry to
the next scheduled pass, because the calling terminal may export keys for another
project under the same names. `swarm tick` run by hand and `doctor measure` read
with the caller's keys, so run them where those keys open the swarm's project.

### Active telemetry

Every working agent record of the watched swarm is an expected binding: name,
life (`name#started_at`), seat, task, harness, profile and, when known, session.
Each scan joins it with:

- **Its exporter on this host**: the session cursor, request and owner files of
  the trace exporter. Generated progress is the transcript size (or the bytes the
  cursor staged); accepted progress is the cursor's accepted bytes. The oldest
  unaccepted source event is the first timestamped transcript record past the
  accepted offset.
- **Langfuse**: `GET /api/public/traces?tags=swarm:<slug>&tags=agent:<name>`
  (core and io fields, newest five) for the correlation attributes on each
  trace; then, for the binding's own session, its newest observations since a
  stored start time watermark (ten rows) and its observation count. Freshness is
  the last accepted source event, never trace creation or Stop time.

| Finding | Raised when |
|---|---|
| `telemetry never exported` | no trace for the binding's session past the grace |
| `exporter backlog` | observations queued in the cursor, or a pending cap overflow, and the oldest unaccepted source event older than the threshold |
| `telemetry stale` | generated progress unaccepted past the threshold while nothing is queued, even with the exporter process alive; or the exporter recorded more accepted observations than Langfuse holds past the threshold since its last acceptance |
| `telemetry misattributed` | a trace tagged with the agent carries another life, seat, task, harness or resolved profile, or the session was accepted with no trace under the agent |
| `trace reader unavailable` | the trace listing or any active read failed or ran out of its budget |
| `trace coverage partial` | the historical backfill has not read every trace's observations yet |

Threshold `AGENTIHOOKS_DOCTOR_ACTIVE_STALE_SECONDS` (60), grace
`AGENTIHOOKS_DOCTOR_ACTIVE_GRACE_SECONDS` (60). A fault is raised at the first
scan after the threshold, so the worst case is the threshold plus the interval.
Idle agents (nothing generated beyond accepted) and finished or retired agents
are never stale. Each finding names agent, seat, task, session and its evidence;
its id carries the fault start (the oldest unaccepted event, the last accepted
Langfuse event, the agent's start or the outage start), so the Doctor master gets
it once while it lasts, it clears on recovery and a new fault raises a new one.
An agent Langfuse could not be read for is judged only on its local exporter.

The read budget is 15 seconds for active bindings, one retry per binding, and
10 seconds per request. Watermarks and the outage start live in
`<swarm home>/<slug>/doctor-telemetry.json`.

### Historical findings

The detector also raises tool error rate, tokens per merged task against the
other merged tasks, long gaps between turns and swarm sessions with no trace.
Thresholds are `AGENTIHOOKS_DOCTOR_TOOL_ERROR_PCT` (20),
`AGENTIHOOKS_DOCTOR_TOOL_ERROR_MIN_CALLS` (10), `AGENTIHOOKS_DOCTOR_TASK_COST_RATIO`
(2), `AGENTIHOOKS_DOCTOR_TURN_GAP_MINUTES` (30) and
`AGENTIHOOKS_DOCTOR_GRACE_MINUTES` (30).

It lists `GET /api/public/traces?tags=swarm:<slug>&fields=core` (at most 20 pages
of 100) and backfills `GET /api/public/observations?traceId=<id>` (pages of 50,
at most 20) newest first within 45 seconds per scan. Each trace's observations are
cached in `<swarm home>/<slug>/doctor-traces/` until the trace's update time
changes, so the backfill completes over successive scans. A trace whose own read
fails is a coverage gap retried after every unread trace; the untraced session
finding is skipped while the listing is incomplete.

`agentihooks doctor <slug> measure <finding>` runs every detector once and prints
that finding's number, and `python -m scripts.doctor.traces_read <slug>` prints
the measures (active bindings, coverage) and findings for one swarm.

### The Langfuse connector

The Doctor master and other agents read traces interactively through the antoncore
Langfuse MCP server (`stacks/langfuse/mcp`), registered on the LiteLLM gateway as
`langfuse_tools`. Its projects come from the `LANGFUSE_PROJECTS` secret and include
`agent-swarm`. Three tools default to that project:

| Tool | Returns |
|---|---|
| `swarm_traces_by_tag(swarm, agent?, task?)` | The swarm's traces, grouped by session |
| `swarm_session_timeline(session_id, include_io?)` | One session's observations in time order, text included on request |
| `swarm_error_latency_summary(swarm, from_timestamp?, to_timestamp?)` | Tool error rate and latency per agent |

A gateway key reaches only the tools its LiteLLM unit allows (`litellm-state`
units). A unit that lists `langfuse_tools` but not these three tool names does
not see them.

### Mounting only the three reads in a profile

The gateway advertises each tool as `<server>-<tool>`, so the three reads are
`langfuse_tools-swarm_traces_by_tag`, `langfuse_tools-swarm_session_timeline` and
`langfuse_tools-swarm_error_latency_summary`. The header `x-mcp-servers:
langfuse_tools` narrows the gateway catalogue to that one server; no header
narrows the tools inside it. A profile declares the connector in its `.mcp.json`
with an `enabled_tools` allowlist of those advertised names (and optionally
`disabled_tools`), keeping every credential as a `${VAR}` reference:

```json
{
  "mcpServers": {
    "langfuse": {
      "type": "http",
      "url": "http://10.10.30.200/mcp/",
      "headers": {
        "Host": "llm.homeofanton.com",
        "Authorization": "Bearer ${MCP_KEY_GATEWAY}",
        "x-mcp-servers": "langfuse_tools"
      },
      "enabled_tools": [
        "langfuse_tools-swarm_traces_by_tag",
        "langfuse_tools-swarm_session_timeline",
        "langfuse_tools-swarm_error_latency_summary"
      ],
      "default_tools_approval_mode": "approve"
    }
  }
}
```

`agentihooks profile render <name> --target claude|codex` turns that into each
harness's own filter:

- **Codex** mounts the declared entry with `enabled_tools` as written,
  `Authorization: Bearer ${VAR}` as `bearer_token_env_var`, any other header
  whose whole value is `${VAR}` as `env_http_headers`, and literal headers as
  `http_headers`. Codex defers MCP tools behind its tool search, so a session
  finds the three through search rather than its initial tool list. Under
  `approval_policy = "never"` Codex refuses an MCP call that needs approval;
  `default_tools_approval_mode = "approve"` on the declaration lets the three
  reads run. Claude never receives this field.
- **Claude** has no per-server allowlist (a `tools` field on a server entry
  drops the server). The render lists the server's advertised tools with the
  declared references resolved in memory, writes the entry without the filter
  fields, and adds `mcp__<server>__<tool>` to `permissions.deny` in the rendered
  `settings.json` for every advertised tool outside the allowlist. A tool the
  server adds later stays visible until the next render.

Each render writes `<target>.mounts.json` beside the profile homes, naming every
declared server as mounted (with its allowlist, and any allowlisted name the
server does not advertise) or unmounted with the reason, and prints each gap.
On Claude an allowlisted server stays unmounted when it is not an HTTP server,
a referenced variable is unset, or the tool listing fails.
