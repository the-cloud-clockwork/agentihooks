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

The Doctor's `trace` detector (`scripts/doctor/traces_read.py`, active bindings
in `scripts/doctor/registry.py`, findings in `scripts/doctor/traces.py`) reads
the same Langfuse public API with `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
and `LANGFUSE_HOST` (default `https://langfuse.homeofanton.com`). Reads for that
exact default origin connect to the internal ingress `10.10.30.200` instead of
the public tunnel, sending the hostname as `Host` and TLS server name so the
certificate is still verified against it; a tunnel outage no longer fails a
read, while a 5xx or a timeout on the internal path still raises `trace reader
unavailable`. The swarm tick runs it with the other detectors every
`AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES` (10); it is the one monitor of trace
freshness. Among ticks, only `swarm tick`, the command the timer unit runs, and
the controller loop run it. A tick another swarm command runs inline (`start`,
`reopen`, `pause`, `stop`, `set`, `restore`, and so `doctor start` and rig
doctor) runs every other detector and leaves telemetry to the next scheduled
pass, because the calling terminal may export keys for another project under the
same names. `swarm tick` run by hand and `doctor measure` read with the caller's
keys, so run them where those keys open the swarm's project.

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
| `wait history partial` | the wait history of an agent with a turn gap over the limit was not read inside the 15 second wait budget or the 55 second step deadline it shares with the trace reads; its turn gaps are not judged that scan |

Threshold `AGENTIHOOKS_DOCTOR_ACTIVE_STALE_SECONDS` (60), grace
`AGENTIHOOKS_DOCTOR_ACTIVE_GRACE_SECONDS` (60). A fault is raised at the first
scan after the threshold, so the worst case is the threshold plus the interval.
Idle agents (nothing generated beyond accepted) and finished or retired agents
are never stale. Each finding names agent, seat, task, session and its evidence;
its id carries the fault start (the oldest unaccepted event, the last accepted
Langfuse event, the agent's start or the outage start), so the Doctor master gets
it once while it lasts, it clears on recovery and a new fault raises a new one.
An agent Langfuse could not be read for is judged only on its local exporter.

Watermarks and the outage start live in
`<swarm home>/<slug>/doctor-telemetry.json`.

### Read budget

Each scan reads every active binding in one round: the read pool has one worker
per binding. A binding takes up to three requests in sequence (its trace
listing, its observations since the watermark and its observation count) and
gets two attempts. The active reads run beside the historical listing and
backfill, which keep their own budget.

| Limit | Value | Applies to |
|---|---|---|
| Active read budget | 15 seconds | every active binding together, from the start of the scan |
| Attempts per binding | 2 | a failed binding read is retried once while the budget lasts |
| Request timeout | 7 seconds | every Langfuse request the Doctor sends |
| Historical budget | 45 seconds | the trace listing and observation backfill |
| Wait history budget | 15 seconds | the wait history of an agent with a turn gap over the limit, also inside the step deadline |
| Trace listing pages | 20 pages | the trace listing, 100 traces per page |
| Backfill pages | 20 pages | each trace's observation backfill, 50 observations per page |
| Active freshness rows | 10 observations | the first page of each binding's observations since its watermark |
| Step deadline | 55 seconds | the trace reads and the wait history together |

The request timeout stays under half the active budget, so one stalled request
leaves room for its retry. When the budget ends, no new attempt starts and every
binding still being read is marked unread. A binding read already in flight
issues no retry, but its remaining requests still run, each up to the 7 second
timeout, and their answers are discarded. These limits are
fixed in the `Budget` defaults of `scripts/doctor/traces_read.py`; no
environment variable changes them.

Every failed read is listed on the `trace reader unavailable` finding as one line
naming the read, its time, the error and the file and line that raised it:

```text
active read of eng-3 failed at 2026-10-18T22:30:04.512Z: ReadTimeout: timed out at <site-packages>/httpx/_transports/default.py:<line>
trace listing failed at 2026-10-18T22:30:09.031Z: HTTPStatusError: Server error '502 Bad Gateway' for url '…' at <site-packages>/httpx/_models.py:<line>
active read budget of 15 seconds spent before eng-7 at 2026-10-18T22:30:15.004Z
```

The reads named are `active read of <agent>`, `exporter progress read of
<agent>`, `trace listing` and `observation read`. The frame is the innermost one
in the traceback, so an error raised while reading the local exporter files
points at this host's file system, while one raised by `httpx` points at the
Langfuse request. A binding that ran out of budget without its own error carries
the budget line instead. A finding shows at most 50 failure lines and counts the
rest in one more line. Its evidence also reads `active bindings read <n> of
<total>`; unread bindings are judged only on their local exporter.

To see one pass by hand, run it where the keys open the swarm's project. It
reads with the caller's `LANGFUSE_*` keys and does not check which project they
serve, and it writes the same state as a scheduled pass: the watermarks and
outage start in `doctor-telemetry.json` and the trace cache.

```bash
python -m scripts.doctor.traces_read <slug>
```

It prints `measures` and `findings` as JSON. `measures.coverage.active` holds
`bindings` and `read`, each entry of `measures.active` carries its own `read`,
and the failure lines sit in the evidence of the `trace reader unavailable`
finding. A healthy pass reads every binding: `read` equals `bindings` and that
finding is absent.

| Symptom | Read |
|---|---|
| `read` below `bindings` with only budget lines | the Langfuse requests are slow; check the response time of the trace listing with the same keys, and the Langfuse server load, before calling it an outage |
| one binding's line ends at an `httpx` frame | that request failed or timed out against Langfuse; the line's error names the status or timeout |
| a line ends at a file system or JSON frame | the exporter cursor or transcript of that session could not be read on this host |
| every read fails with the same error | the keys, host or ingress route are wrong; `LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set` means the keys the reader was given are missing or empty |

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
that finding's number. It refuses a finding id it never saw, a gone finding whose
agent is no longer live and an absent finding while a detector failed, naming
each failed detector, rather than print zero. `measure telemetry-stale` (or `exporter-backlog`) reads every
active binding and prints `count`, `unknown` (no exporter progress) and `max`
unaccepted seconds; any other class name prints its current findings, with
`unknown` counting the detectors that failed, and is refused when a detector
failed and no finding of the class is current. A `failed-spawn/<task>` id counts
the spawn journal over its window, so zero there is a real reading for a task
on the watched ledger or the master; any other task is refused.
To run one trace pass by hand, see [Read budget](#read-budget).

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

## The swarm Langfuse summary pass

Each scheduled tick (`swarm tick`, the command the timer unit runs, and the
controller loop) asks the internal tool gateway for one Langfuse summary of the
swarm and turns breaching accounts into health findings. A tick another swarm
command runs inline skips it.

### What it reads

The reader opens a streamable HTTP MCP session to `LANGFUSE_MCP_URL` with three
headers:

| Header | Value |
|---|---|
| `Authorization` | `Bearer $MCP_KEY_GATEWAY` |
| `Host` | `llm.homeofanton.com` |
| `x-mcp-servers` | `langfuse_tools` |

It calls `langfuse_tools-swarm_error_latency_summary` with the swarm slug, the
project `agent-swarm` and a window of the last hour. The pass runs at most once
every ten minutes per swarm, and the whole read is bounded at 30 seconds.

### The encrypted internal route

The public gateway name sits behind Cloudflare Access and refuses the gateway
key, so the reader connects to the gateway's LAN ingress address instead and
routes by the `Host` header. The ingress serves TLS for the gateway name with a
valid `*.homeofanton.com` certificate. The reader's HTTP client takes the TLS
server name from the `Host` header, so a request to
`https://10.10.30.200/...` sends `llm.homeofanton.com` as SNI and verifies the
certificate against that name while it connects to the address. The bearer
credential therefore travels encrypted on the home network.

The scheme and address come from `LANGFUSE_MCP_URL` alone; nothing in code picks
them. An `http://` endpoint still works and sends the key in clear text, so
every configured value in git is `https`:

| Reader | Where the endpoint is set | Value |
|---|---|---|
| Deployed swarm controller | antoncore `k8s/values-overlays/agentihooks-swarm/values.yaml`, `controller.env` | `https://10.10.30.200/toolset/swarm-controller/mcp` |
| Local sampler (timer ticks on a workstation) | agentihooks-bundle `profiles/anton-base` env for Claude, Codex and Copilot | `https://10.10.30.200/mcp/` |

The bundle and antoncore each have a test that pins the configured scheme to
`https`. The agentihooks Helm chart ships `controller.env.LANGFUSE_MCP_URL`
empty, so a deployment without an overlay value runs with sampling off.

The overlay gives `MCP_KEY_GATEWAY` only to the controller, from the
`swarm-controller-gateway` Secret (key `key`) through `controller.secretEnv`.
The switch to the encrypted route (antoncore pull request 889, agentihooks
issue 3731) changed only the scheme and the reader's TLS server name; the
credential and the `agent-swarm` project stayed as they were.

The Grafana alert read in the same tick reuses this endpoint, client and key
with `x-mcp-servers: grafana`, so it uses the encrypted route too. Unsetting
`LANGFUSE_MCP_URL` turns off both reads; unsetting `MCP_KEY_GATEWAY` makes both
fail silently.

### Configuration

```yaml
# antoncore k8s/values-overlays/agentihooks-swarm/values.yaml
controller:
  env:
    LANGFUSE_MCP_URL: https://10.10.30.200/toolset/swarm-controller/mcp
  secretEnv:
    - name: MCP_KEY_GATEWAY
      secretName: swarm-controller-gateway
      key: key
      optional: true
```

The bundle sets the local value in `profiles/anton-base/.claude/settings.overrides.json`:

```json
{ "env": { "LANGFUSE_MCP_URL": "https://10.10.30.200/mcp/" } }
```

and the same value in the Codex (`config.overrides.toml`) and Copilot
(`settings.overrides.json`) layers. `agentihooks init` also writes the
profile's `LANGFUSE_MCP_URL` into `~/.agentihooks/.env`, between
`# BEGIN agentihooks sampler environment` and
`# END agentihooks sampler environment`, when that file does not already set
it; a value already set there wins and is never overwritten. Change the value
in git and let CI deploy it; an edited Pod is replaced on the next rollout.

### Findings

A row breaches when an agent with at least 10 observations has an error rate
over 0.2, or an agent with at least 3 turns has a turn latency p95 over ten
minutes. Breaches are grouped by the agent's account into one standing
`langfuse_account_<account>` finding, which names each agent, task, value and
threshold. After a successful read, a finding clears when its account no
longer breaches, except an account whose agents had enough turns but no latency
figure, which keeps its finding until a read can judge it. The tick prints
`Langfuse summary read: <n> breaching accounts`; `swarm tick` prefixes each
line with the swarm slug, as in `<slug>: Langfuse summary read: ...`.

With `LANGFUSE_MCP_URL` unset the tick prints `Langfuse sampling is off:
configure LANGFUSE_MCP_URL` once, then stays quiet and reads nothing until the
variable is set. A failed read (refused TLS, timeout, tool error) stores the
exception's type name under `error` in the swarm's `langfuse-summary` Redis
key and changes no finding; the next attempt waits out the ten minute interval.

### Checking the route

1. The ingress presents a certificate valid for the gateway name:

   ```bash
   openssl s_client -connect 10.10.30.200:443 -servername llm.homeofanton.com \
     -verify_return_error </dev/null 2>&1 | grep -E "subject=|Verify return code"
   ```

   Observed on 2026-10-19 from a home network workstation:
   `subject=CN = *.homeofanton.com`, issuer Let's Encrypt, and
   `Verify return code: 0 (ok)`.
2. The gateway answers over that route. Without a key it refuses with 401, which
   proves the TLS session and routing work:

   ```bash
   curl -s -o /dev/null -w "%{http_code} %{ssl_verify_result}\n" \
     --connect-to llm.homeofanton.com:443:10.10.30.200:443 \
     https://llm.homeofanton.com/toolset/swarm-controller/mcp
   ```

   Observed on 2026-10-19 from the same workstation: `401 0`, a refusal for
   the missing key with certificate verification passed.
3. The deployed value is `https`: read `controller.env.LANGFUSE_MCP_URL` in the
   antoncore overlay on the branch ArgoCD tracks.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Langfuse sampling is off` in the tick output | `LANGFUSE_MCP_URL` is unset where the tick runs | Set it in the overlay or bundle profile, then redeploy or rerun `agentihooks init` |
| No `Langfuse summary read` line and no findings | The read failed; `error` in the `langfuse-summary` key names the exception type | Run the two checks above: a failed first check points at the ingress or its certificate, a failed second at routing; with both passing, check `MCP_KEY_GATEWAY` is set for the reader |
| No tick line, `error` is `KeyError` | `MCP_KEY_GATEWAY` is unset; on the controller the `swarm-controller-gateway` Secret is optional, so a missing Secret starts the Pod without the key | Create the Secret with key `key`; the next pass after the ten minute interval reads |
| Certificate verification fails | The ingress certificate does not cover the TLS server name `llm.homeofanton.com` (not observed; the first check above shows it) | Run the first check; keep `Host: llm.homeofanton.com`, since the reader takes the TLS server name from it |
| The gateway refuses the key | The endpoint names the public gateway address behind Cloudflare Access | Use the LAN ingress address with `https` |
| Grafana bottleneck findings never appear | The Grafana read failed, or `LANGFUSE_MCP_URL` or `MCP_KEY_GATEWAY` is unset; that read prints nothing and stores no error | Run the two checks above and confirm both variables are set where the tick runs |
