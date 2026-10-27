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
wins. Codex sessions on the app-server transport are the exception: their launch
sets the key pair from these files over any inherited one (see
[Traces in another Langfuse project](#traces-in-another-langfuse-project)).

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

## Traces in another Langfuse project

Langfuse files spans under the project the key pair belongs to. A session that
exports with another project's keys gets an accepted cursor and no failure log
line, yet its trace is missing from the swarm's project. The Doctor reports it as
a [`telemetry misattributed`](#active-telemetry) finding of the unattributed
kind: `session <id> accepted <n> observations, none under agent:<name>`.

### Cause found on 19 October

Doctor finding `telemetry-misattributed/engineer@323133-2054` (issue
[#4899](https://github.com/the-cloud-clockwork/agentihooks/issues/4899)) named a
watched Codex agent whose session `01a154bc-0fa0-7c53-943e-5816332b59c1` had an
accepted cursor with 450 spans and no trace in `agent-swarm`.

- The hook reads `OTEL_LANGFUSE_PUBLIC_KEY` and `OTEL_LANGFUSE_SECRET_KEY`, falls
  back to `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, and never lets an
  AgentiHooks env file override a value already in the process environment.
- The herdr server process held a `LANGFUSE_*` pair for the project `antoncore`
  from boot, and the pane shells and launchers it started inherited that pair.
  None of them set the `OTEL_LANGFUSE_*` names.
- A Codex session on the app-server transport received its environment through
  the Codex router's routed child environment, which passes the launching
  environment through. The Codex hook wrapper sets the Langfuse switch, not the
  keys. Those sessions therefore exported with the `antoncore` pair.

Evidence recorded on the cause task:

| Check | Result |
|---|---|
| Trace `8550912e0cd2eeaa4958b122d1545fd3` (session `01a154bc`) with the `agent-swarm` keys | 404 |
| The same trace with the `antoncore` keys | 200, tags `agent:engineer@323133-2054` and `swarm:rig-grade-swarm`, 450 observations |
| Codex sessions on the app-server transport started in the watched swarm over 48 hours | 20 of 20 missing from `agent-swarm`, all 20 present in `antoncore` |
| Codex sessions under profile homes in both swarms over the same 48 hours | 202 of 202 traced in `agent-swarm` |

### Pinned keys at app-server launch

Since pull request
[#4903](https://github.com/the-cloud-clockwork/agentihooks/pull/4903),
`python -m scripts.codex_session serve` routes the Codex account, then sets
`OTEL_LANGFUSE_PUBLIC_KEY` and `OTEL_LANGFUSE_SECRET_KEY` in the session's
environment from the AgentiHooks env files
(`profile_telemetry.agentihooks_langfuse_keys`):

- For each key it reads the `OTEL_LANGFUSE_*` name first, then the
  `LANGFUSE_*` name.
- Files are read as the hook reads them: `~/.agentihooks/.env`, then the other
  `~/.agentihooks/*.env` files in sorted order, first assignment wins.
- Because the hook reads the `OTEL_` names first, the pinned pair wins over an
  inherited `LANGFUSE_*` pair and over a stale inherited `OTEL_` value.
- When the files hold neither name for a key, that key is left as inherited.

With the Anton env files below, every app-server Codex session exports to
`agent-swarm` whatever the herdr server carries:

```bash
# ~/.agentihooks/langfuse.env
AGENTIHOOKS_LANGFUSE_ENABLED=1
LANGFUSE_PUBLIC_KEY=<agent-swarm public key>
LANGFUSE_SECRET_KEY=<agent-swarm secret key>
```

The pin applies at launch. A session already running keeps the keys it started
with, and traces already exported stay in the project that accepted them. Claude
sessions and Codex terminal sessions are not pinned: for them a `LANGFUSE_*`
pair in the launching environment still wins over the env files.

The Doctor's reader uses the same env file keys and refuses to measure unless
they open the watched swarm's project.

### Finding where a trace went

1. Derive the session's trace id. The exporter computes it from the session id,
   so it is the same in every project:

   ```bash
   cd ~/dev/tcc-ecosystem/agentihooks
   SESSION="<session id>"
   TRACE=$(~/dev/tcc-ecosystem/.venv/bin/python -c "import sys; from hooks.observability.agent_trace import trace_id; print(format(trace_id(sys.argv[1]), '032x'))" "$SESSION")
   echo "$TRACE"
   ```

2. Ask the swarm's project for it. `200` means the trace is there; `404` means
   another project accepted it:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" "https://langfuse.homeofanton.com/api/public/traces/$TRACE"
   ```

3. Check which project each key pair opens. The shell's pair:

   ```bash
   curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" https://langfuse.homeofanton.com/api/public/projects | jq -r '.data[].name'
   ```

   The pair the app-server launch pins, from the env files:

   ```bash
   cd ~/dev/tcc-ecosystem/agentihooks
   ~/dev/tcc-ecosystem/.venv/bin/python - <<'EOF'
   import httpx
   from scripts.profile_telemetry import agentihooks_langfuse_keys
   keys = agentihooks_langfuse_keys()
   auth = (keys["OTEL_LANGFUSE_PUBLIC_KEY"], keys["OTEL_LANGFUSE_SECRET_KEY"])
   print([p["name"] for p in httpx.get("https://langfuse.homeofanton.com/api/public/projects", auth=auth, timeout=20).json()["data"]])
   EOF
   ```

   Both print `agent-swarm` on the Anton workbench. Neither command prints a key.

4. Test which key names a launching shell carries, without printing values:

   ```bash
   for name in LANGFUSE_PUBLIC_KEY OTEL_LANGFUSE_PUBLIC_KEY; do test -n "${!name:-}" && echo "$name set" || echo "$name unset"; done
   ```

5. Repeat step 2 with a pair that opens another project to confirm the trace
   landed there.

### Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Accepted cursor, trace 404 in `agent-swarm`, app-server Codex session started before #4903 | Inherited `LANGFUSE_*` pair for another project | The trace stays in that project. Sessions launched since #4903 pin the env file pair. |
| Accepted cursor, trace 404 in `agent-swarm`, Claude or Codex terminal session | A `LANGFUSE_*` or `OTEL_LANGFUSE_*` pair for another project in the launching environment | Unset the pair, or set the swarm's pair under the `OTEL_` names, in the environment that starts the herdr server or the launcher, then relaunch the session. |
| App-server Codex session still exports elsewhere after #4903 | The env files hold no key pair, so the inherited pair is kept | Put the swarm's pair in `~/.agentihooks/.env` or a companion `*.env` file; step 3 shows the project it opens. |
| Step 3 prints another project for the env file pair | The env files hold another project's keys | Replace them with the swarm project's pair; new launches pick them up. |
| `KeyError` from the step 3 snippet | The env files hold no `OTEL_LANGFUSE_*` or `LANGFUSE_*` key | Add the pair to an env file. |

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
same names. `swarm tick` run by hand reads with the caller's keys, so run it
where those keys open the swarm's project.

`doctor measure` ignores the caller's `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY`. It reads with the key pair in the AgentiHooks env files,
loaded by `profile_telemetry.agentihooks_langfuse_keys` (`OTEL_LANGFUSE_*`
first, else `LANGFUSE_*`, first assignment wins across the files), against the
default host; `traces_read.installed_env` swaps them in. Before any read,
`traces_read.measured_client` asks Langfuse which project the keys serve and
refuses to read unless the answer is exactly `agent-swarm`, failing with
`the installed Langfuse keys serve project <name>, not the watched swarm project
agent-swarm`, or with `Langfuse project identity is unavailable` when the
question fails or names no project. A refusal fails the `trace` detector: its
error is printed on stderr and the measure counts it among the failed
detectors. The caller's terminal keys never change what `doctor measure` reads.

### Internal ingress for Doctor reads

The reader chooses its route from `LANGFUSE_HOST` after removing trailing
slashes. The default needs no route setting:

| Reader host | Connection |
|---|---|
| Unset, empty, or `https://langfuse.homeofanton.com` (also with a trailing slash) | HTTPS to `10.10.30.200:443`, with `langfuse.homeofanton.com` as HTTP `Host` and TLS server name |
| Another hostname, scheme or explicit port, such as `https://langfuse.example.com` or `https://langfuse.homeofanton.com:8443` | Directly to the configured URL |

TLS verification stays enabled. Keep the hostname in `LANGFUSE_HOST`: setting
it to the ingress IP selects a direct request and loses the hostname used for
certificate verification. The ingress address comes from the built-in
`langfuse_route({})` default; changing the exporter's `OTEL_LANGFUSE_ENDPOINT`
does not change this reader route. The project key pair is passed unchanged as
HTTP Basic authentication, and the [read budget](#read-budget) still applies.

For a manual reader pass on Anton, use a shell with the watched project's keys
already supplied, then run:

```bash
cd ~/dev/tcc-ecosystem/agentihooks
LANGFUSE_HOST=https://langfuse.homeofanton.com \
  LANGFUSE_PUBLIC_KEY="$SWARM_LANGFUSE_PUBLIC_KEY" \
  LANGFUSE_SECRET_KEY="$SWARM_LANGFUSE_SECRET_KEY" \
  ~/dev/tcc-ecosystem/.venv/bin/python -m scripts.doctor.traces_read rig-grade-swarm
```

`SWARM_LANGFUSE_PUBLIC_KEY` and `SWARM_LANGFUSE_SECRET_KEY` above are references
to keys supplied by your secret manager. This command uses the internal route,
prints `measures` and `findings`, and updates the reader's watermarks and cache.
For another installation, replace the host and key references with that
installation's HTTPS origin and project pair; that origin is read directly.
The manual reader does not validate the project. For a measurement with the
installed keys and the `agent-swarm` project check described above, use:

```bash
agentihooks doctor rig-grade-swarm measure trace-reader-unavailable
```

This measure command always uses the default host, regardless of the caller's
`LANGFUSE_HOST`.

#### Checking the internal connection

Run this read-only probe from the host that runs the Doctor. It needs no
project keys and keeps the same hostname and certificate verification:

```bash
curl --noproxy '*' --resolve langfuse.homeofanton.com:443:10.10.30.200 \
  --connect-timeout 5 --max-time 10 --silent --show-error \
  --output /dev/null --write-out 'status=%{http_code} tls=%{ssl_verify_result}\n' \
  https://langfuse.homeofanton.com/api/public/health
```

`status=200 tls=0` confirms a healthy response and a verified certificate from
the ingress. It does not confirm that the reader's keys open the right project
or that trace queries finish within their budget.

| Symptom | Check |
|---|---|
| Public URL returns a tunnel 502, internal health probe succeeds | Default-origin Doctor reads bypass the tunnel. Compare the finding's request URL and time with the internal path before attributing it to the tunnel. |
| Internal connection times out or refuses the connection | The Doctor host must reach `10.10.30.200:443`; check that path and the ingress. There is no fallback to the public tunnel. |
| Certificate verification fails | Check the certificate served for `langfuse.homeofanton.com`; keep verification enabled and the hostname configured. |
| Langfuse returns 5xx through the internal path, or trace requests time out | These remain read failures and can raise `trace reader unavailable`; inspect the [failure lines](#read-budget) and backend health. |
| Health succeeds but measurement refuses the project | Follow [Finding where a trace went](#finding-where-a-trace-went); health alone does not validate project identity. |

Pull request [#5353](https://github.com/the-cloud-clockwork/agentihooks/pull/5353)
proved the route with a public tunnel returning 502 and an internal endpoint
returning 200. Its CI proofs went from
[red](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39486962592)
to [green](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39487595146),
including checks that internal 503 responses and timeouts still raise the
reader finding. Host memory paging was a hypothesis, not an established cause
of that incident.

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
| `telemetry never exported` | no trace for the binding's session past the grace, once the agent has completed a turn (a recorded Stop or session end, or a second prompt in the exporter cursor) or has worked past the first turn bound |
| `exporter backlog` | observations queued in the cursor, or a pending cap overflow, and the oldest unaccepted source event older than the threshold |
| `telemetry stale` | generated progress unaccepted past the threshold while nothing is queued, even with the exporter process alive; or the exporter recorded more accepted observations than Langfuse holds past the threshold since its last acceptance |
| `telemetry misattributed` | a trace tagged with the agent carries another life, seat, task, harness or resolved profile, or the session was accepted with no trace under the agent |
| `telemetry start unknown` | the binding has no positive start; see [Agent start](#agent-start) |
| `trace reader unavailable` | the trace listing or any active read failed or ran out of its budget |
| `trace coverage partial` | the historical backfill has not read every listed trace's observations yet; see [Trace coverage](#trace-coverage) |
| `wait history partial` | the wait history of an agent with a turn gap over the limit was not read inside the 15 second wait budget or the 55 second step deadline it shares with the trace reads; its turn gaps are not judged that scan |

Threshold `AGENTIHOOKS_DOCTOR_ACTIVE_STALE_SECONDS` (60), grace
`AGENTIHOOKS_DOCTOR_ACTIVE_GRACE_SECONDS` (60), first turn bound
`AGENTIHOOKS_DOCTOR_FIRST_TURN_MINUTES` (60): Claude sessions export at a turn's
Stop, so an agent still in its first turn is not judged never exported. A fault is raised at the first
scan after the threshold, so the worst case is the threshold plus the interval.
Idle agents (nothing generated beyond accepted) and finished or retired agents
are never stale. Each finding names agent, seat, task, session and its evidence;
its id carries the fault start (the oldest unaccepted event, the last accepted
Langfuse event, the agent's start or the outage start), so the Doctor master gets
it once while it lasts, it clears on recovery and a new fault raises a new one.
An agent Langfuse could not be read for is judged only on its local exporter.

Watermarks and the outage start live in
`<swarm home>/<slug>/doctor-telemetry.json`.

#### Agent start

The grace, the first turn bound and the age in `telemetry never exported` are
all measured from the binding's start. The Doctor takes it from the agent
record in this order:

1. `started_at`, the spawn time, when it is above zero.
2. `launched_at`, the time the swarm launched the agent, when it is above
   zero. A hive agent can carry a zero `started_at` while its launch time is
   recorded.
3. Otherwise the start is unknown.

A zero, negative, missing or null value counts as absent. The life keeps the
raw `started_at`, so a hive agent resolved from its launch time still has life
`name#0` and its traces correlate as before. The `telemetry never exported`
and `telemetry misattributed` ids use the resolved start; the
`telemetry count drift` id keeps the raw life.

An unknown start raises `telemetry start unknown` instead of any other active
telemetry finding for that binding. The Doctor does not apply the grace, does
not compute an age and does not judge the binding never exported, stale or
misattributed until a start is recorded. The finding has measure 1 and its id
is the agent name alone, so the Doctor master gets one notice per agent while
the gap lasts.

| Agent record | Resolved start | Result at a scan |
|---|---|---|
| `started_at` 60 seconds ago, `launched_at` 10 minutes ago | spawn time | inside the 60 second grace: no finding |
| `started_at` 0, `launched_at` 60 seconds ago | launch time | inside the grace: no finding |
| `started_at` 0, `launched_at` 3700 seconds ago, past the first turn bound; read this scan, no trace, nothing accepted locally, no backlog | launch time | `telemetry never exported` after 3700 seconds |
| `started_at` 0 or absent, `launched_at` 0 or absent | none | `telemetry start unknown` |

Example finding for an agent with neither time recorded:

```json
{
  "kind": "telemetry start unknown",
  "subject": "engineer@hive-1",
  "summary": "working agent start is unknown",
  "evidence": [
    "agent engineer@hive-1",
    "seat unknown",
    "task unknown",
    "session unknown",
    "no positive spawn or launch timestamp; launch grace cannot be established"
  ],
  "threshold": "an observed start is required to judge active telemetry"
}
```

Before pull request
[#5670](https://github.com/the-cloud-clockwork/agentihooks/pull/5670), a zero
`started_at` was subtracted directly, so hive agents were reported as never
exported with ages of decades and skipped the grace. If this finding appears,
check the agent record in the swarm store: a working agent needs a positive
`started_at` or `launched_at`. The order is fixed in code; no setting changes
it.

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
| Step deadline | 55 seconds | the wait history reads, counted from the start of the trace step; the trace reads keep their own budgets |

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
| reads time out while the swarm tick also logs Redis socket timeouts | the shared host under Langfuse's Redis is stalled; see [Langfuse and the swarm Redis time out together](#langfuse-and-the-swarm-redis-time-out-together) |

### Langfuse and the swarm Redis time out together

Langfuse keeps its queue and cache in a Redis outside the cluster: the
`dataplane_redis` container on the Anton Unraid host, `10.10.30.130:6379`. The
public tunnel to `langfuse.homeofanton.com` is the `cloudflared` container on
the same host, and the k3s VMs `k8s-master`, `k8s-worker-1` and `k8s-worker-2`
are libvirt guests there too. When that host runs out of memory every one of
these stalls at once, so Langfuse, the edge and anything scheduled on those VMs
fail in the same minutes even though the pods themselves never restart.

#### What each path shows

| Path | Signature during a host stall |
|---|---|
| Public Langfuse URL | Cloudflare answers 530 (error 1033), while on 20 October `grafana` and `sonar` on the same domain still answered; `cloudflared` logs `timeout: no recent network activity` on all four QUIC connections, then re-registers |
| Langfuse server (`langfuse-0` in `anton-prod`) | Redis `Command timed out` and socket timeout errors; the pod keeps running |
| Langfuse Redis (`dataplane_redis`) | `Asynchronous AOF fsync is taking too long (disk is busy?)`, and a `Saving...` line followed many seconds later by `Background saving started`: the fork blocked the main thread |
| Swarm tick | `alias lookup failed for ...: Timeout reading from socket` in the user journal, from `scripts.swarm.naming`, when the swarm Redis runs on one of the Anton VMs. The lookup falls back to the plain name, so the tick keeps going |
| Doctor trace reads | read timeouts and 5xx answers, raised as `trace reader unavailable` |
| Other containers on the host | `anton_autoheal` restarts unrelated containers whose health checks timed out, and its own alert posts get `error code: 1033` |

#### Checking for a host stall

Confirm which Redis Langfuse holds connections to. `/proc/net/tcp` prints the
remote address in little endian hex; `821E0A0A:18EB` is `10.10.30.130:6379` and
state `01` is established:

```bash
KUBECONFIG=~/.kube/config-k3s kubectl -n anton-prod exec langfuse-0 -- \
  cat /proc/net/tcp | awk '$3 ~ /:18EB$/ {print $3, $4}' | sort | uniq -c
```

Observed on 23 October: `6 821E0A0A:18EB 01`, six established connections to
the Anton host.

Count the swarm tick's Redis timeouts in the window:

```bash
journalctl --user -t agentihooks-swarm --since "2026-10-20 11:00" --until "2026-10-20 11:40" \
  | grep -c "alias lookup failed"
```

Then read the host itself, read only, over SSH
(`ssh -i ~/.ssh/anton_id_ed25519 root@10.10.30.130`):

```bash
free -m
swapon --show
grep -E "^(allocstall|compact_stall|oom_kill)" /proc/vmstat
dmesg -T | grep -i "out of memory"
virsh list
for vm in k8s-master k8s-worker-1 k8s-worker-2 automation-vm; do virsh dommemstat "$vm" | grep -E "actual|rss"; done
docker logs --since "2026-10-20T11:00:00" --until "2026-10-20T11:40:00" cloudflared 2>&1 | grep "no recent network activity"
docker logs --since "2026-10-20T11:00:00" --until "2026-10-20T11:40:00" anton_autoheal 2>&1 | grep unhealthy
docker logs --since "2026-10-20T11:00:00" --until "2026-10-20T11:40:00" dataplane_redis 2>&1 \
  | grep -E "fsync is taking too long|Saving|Background saving started"
```

A stalled host shows swap fully used, little available memory, large
`allocstall` counts and the log lines in the table above in the same minutes.
The host has no node exporter in Prometheus, so these commands are the only
host history.

#### Cause found on 20 October

Swarm task t575 followed an outage from 11:20 to 11:27 UTC on 20 October:
`langfuse.homeofanton.com` answered 530 at 11:24 and 200 again at 11:26:48,
`langfuse-0` logged 62 Redis timeouts in the hour to 11:24, and the swarm tick
logged 97 `alias lookup failed ... Timeout reading from socket` lines between
11:20 and 11:30 against the swarm Redis, then on `k8s-worker-2`. Read on the host
that day:

| Check | Result |
|---|---|
| Physical memory | 60 GB; the three k3s VMs fully resident at 14.4, 14.5 and 14.6 GB, each with a 14 GiB maximum, `automation-vm` 2.7 GB of 8 GiB |
| Swap | the 4 GB swapfile 100 percent used |
| `/proc/vmstat` | `allocstall_normal` 3.48 million, `allocstall_movable` 6.20 million, `compact_stall` 1.62 million |
| `dmesg` | a global OOM kill of the `k8s-worker-1` qemu process on 19 October at 09:09:25 |
| `dataplane_redis` | slow AOF fsync at 11:20:04; a 20.5 second fork for a 6 MB dataset from 11:25:26 to 11:25:47 |
| `cloudflared` | all four QUIC connections timed out from 11:24:47 to 11:26:26, re-registered 11:25:52 to 11:25:55 |
| `anton_autoheal` | seven unrelated containers restarted between 11:22:29 and 11:27:03 |

The root cause was memory overcommit on the Anton host. Memory was already
exhausted, so any burst stalled everything on it. The likely trigger, correlated
but not proven, was a CI runner pod created on `k8s-worker-2` at 11:19:08 that
ran a Terraform job from 11:20:24 to 11:26:24, while that node's disk writes rose
from about 0 to 46 MB/s.

#### Fix and result

The fix is in antoncore, not in AgentiHooks:
[antoncore#998](https://github.com/the-cloud-clockwork/antoncore/issues/998)
holds the diagnosis and
[antoncore#1024](https://github.com/the-cloud-clockwork/antoncore/pull/1024),
merged on 20 October, sizes the VMs in `VM_DEFS` of `automation/vm-provision.sh`
at 36 GiB in all: `k8s-master` 8G, `k8s-worker-1` and `k8s-worker-2` 10G,
`automation-vm` 8G. `bash automation/vm-provision.sh --memory`, run from a clean
antoncore `dev` checkout, applies them: persistent maximum and current memory,
and the live balloon of a running VM. The VM sizing and its budget are
described in antoncore `docs/2A-KUBERNETES.md`.

Measured on the host after the change was applied at 23:43 UTC on 20 October:
available memory rose from 941 MiB to 16852 MiB. Over the next hour, 62
one-minute samples showed available memory never below 15098 MiB, swap never
full, load at most 22, no new OOM kill and no swap traffic at the end.

Two things have changed since that bear on reading this symptom:

- Doctor reads of the default host go through the internal ingress, not the
  tunnel (see [How the Doctor reads traces](#how-the-doctor-reads-traces)), so a
  530 at the edge alone no longer fails a read. A stall of `dataplane_redis`
  still does, through the Langfuse server.
- On 23 October the swarm Redis pod `agentihooks-swarm-redis-tls-0` and
  `langfuse-0` both ran on `worker-001`, which is not one of the Anton VMs
  (`virsh list` on the host names only `k8s-master`, `k8s-worker-1`,
  `k8s-worker-2` and `automation-vm`), so a host stall no longer stalls the
  swarm Redis with it. Check placement with
  `kubectl -n agentihooks-swarm get pod -o wide` before assuming both paths share
  a host.

### Wait history

A turn gap finding counts only the minutes between two turns that fall outside
the agent's declared waits (`agentihooks swarm <slug> wait`). After the trace
reads, the detector reads those waits from Redis with `idle.wait_history`, one
hash per agent name and per alias of that name. `traces_read.waits` reads them
inside their own budget, and `traces.gap_agents` picks the agents that need
them.

**Which agents are read.** Only agents whose traces hold a gap between two
turns longer than `AGENTIHOOKS_DOCTOR_TURN_GAP_MINUTES` (30), counted in whole
minutes. An agent with no long gap cannot raise a turn gap finding, so its wait
history is never read. The agents are read in this order:

1. agents with an active binding in this scan, the live agents
2. then the agent whose newest long gap ends latest
3. then by name

**When the reads stop.** The step clock starts when the trace step starts,
before the Langfuse reads. The wait reads stop at whichever comes first:

| Limit | Value | Counted from |
|---|---|---|
| Wait history budget | 15 seconds | the first wait read |
| Step deadline | 55 seconds | the start of the trace step |

The clock is checked before each agent. A read that has started finishes, and
no new read starts once the deadline has passed. The trace reads keep their own
budgets ([Read budget](#read-budget)) and are not cut by the step deadline. The
deadline only limits how long the wait reads may run after them. If the trace
reads end 20 seconds into the step, the wait reads stop at 35 seconds, when
their 15 second budget ends. If the trace reads end at 50 seconds, the wait
reads stop at 55 seconds. If the trace reads end at 55 seconds or later, no wait
history is read. Both values are `Budget.wait_seconds` and
`Budget.step_seconds` in `scripts/doctor/traces_read.py`, and no environment
variable changes them. Raising `AGENTIHOOKS_DOCTOR_TURN_GAP_MINUTES` leaves
fewer agents to read.

**Agents left unread.** An agent whose wait history was not read gets no turn
gap judgement in that scan, so no turn gap finding is raised for it. The scan
raises one `wait history partial` finding for the swarm instead.
The finding's subject is the swarm slug and its number is the count of unread
agents:

```text
wait history of 6 agents with turn gaps over 30 minutes not read in budget
unread: eng-3, eng-7, ci-1, eng-12, eng-4
live agents unread: 2
turn gap findings skip agents whose wait history was not read
each Doctor scan reads the wait history again within its budget
```

The first line is the title. The `unread` line names the first five unread
agents in read order. The threshold reads `every agent with a turn gap over the
limit has its wait history read`. The finding is absent when every agent that
needs its wait history was read. Each scan picks the agents again, so the next
scan retries the unread ones.

#### Live agents first

An agent counts as live when the watched swarm's store lists it as a working
agent in this scan, which gives it an expected binding under
[Active telemetry](#active-telemetry). It counts as live whether or not its
binding's telemetry was read in that scan. Every other agent with traces is
retired: its traces stay in the cache and can still hold a long gap.

`traces.gap_agents` sorts the agents with a long gap by three keys, in order:

1. live before retired
2. inside each group, the agent whose newest long gap ends latest first
3. then by name

`traces_read.waits` reads down that list and stops at the wait budget or the
step deadline, so the unread agents are always the tail of the list. A live
agent is left unread only when every retired agent with a long gap was left
unread too. For example, with 3 live and 100 retired agents with long gaps
and time for 40 reads, the 3 live agents and the 37 retired agents whose newest
gap ends latest are read. The 63 left unread are all retired, and the finding
reads `live agents unread: 0`.

The `live agents unread` evidence line of `wait history partial` counts every
live agent among all unread agents, not only the five the `unread` line names.
A value above 0 means even the live agents did not fit in the budget. Turn gap
findings for those live agents are missing from that scan, and no retired agent
was read at all.

| `live agents unread` | Meaning | Action |
|---|---|---|
| `0` | every live agent with a long gap was judged; only retired agents were skipped | none for the live swarm; retired agents are retried on later scans |
| above `0` | the wait reads covered fewer agents than the live agents with long gaps | read the trace step time below; the Langfuse reads or Redis used up the step |

The finding's number, which `doctor measure wait-history-partial/<slug>`
prints, stays the count of all unread agents, live and retired. The live count
appears only in the evidence line.

**Why live agents come first.** Pull request #5176 bounded the wait reads and
read the agents newest gap first. The next scans on rig-grade-swarm raised
`wait history partial` with 106 agents with turn gaps over 30 minutes not read
in budget, and every agent the finding named was retired. A live agent whose
newest gap ended earlier than those retired agents' gaps would have been
skipped behind them. Pull request #5229 (issue #5226) put live agents first
and added the `live agents unread` line.

The three seam tests failed on the old order in Proofs run
[39410184551](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39410184551)
(order `engineer, ci, dev` where `dev, engineer, ci` was expected, and the live
line missing) and passed on the fix in Tests run
[39411235265](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39411235265).
Ten passes of the trace detector on rig-grade-swarm before the fix and ten with
it, on 20 October 2026, each found 0 live agents left unread. In both windows
no live agent had a turn gap over 30 minutes and every wait read finished in
budget, so the measurement could not move, and the CI tests are the proof of
the order.

The tests are in `tests/doctor/test_turn_gap_waits.py`:
`test_live_agents_are_read_before_retired_ones_each_newest_gap_first` pins the
order, `test_the_partial_read_counts_the_live_agents_left_unread` pins the
count, and
`test_an_agent_whose_wait_history_was_not_read_is_not_judged_and_the_read_is_partial`
pins the full evidence text.

#### Why the wait reads are bounded

Before this change, the step read the wait history of every traced agent,
whatever its gaps. On rig-grade-swarm on 20 October 2026, `doctor.trace` took
51 to 73 seconds per pass until 04:42 UTC, then 200 to 300 seconds. The last
three passes before 06:54 UTC took 125 seconds, 238 seconds and over 450
seconds. The over 450 second pass was still running when the 450 second tick
deadline stopped the tick. No Doctor code had changed in that window. A probe
of one pass, using the real code and a copy of the trace cache, timed each
part:

| Part | Seconds |
|---|---|
| Langfuse trace listing and backfill | 15.6 |
| Active binding reads, 13 of 13 | 5.4 |
| Wait history of all 1998 traced agents, one after another | 118.4 |
| Wait history of the 119 agents with a gap over 30 minutes | 6.0 |

Pull request #5176 (issue #5175) limited the wait reads to the agents with a
long gap and gave them the budget above. The new tests failed on the old code
in Proofs run
[39389821466](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39389821466)
and passed on the fix in Tests run
[39391541421](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39391541421).
Pull request #5229 later moved live agents to the front of the read order. The
command under [Reading the trace step time](#reading-the-trace-step-time), run
at 15:07 UTC on 23 October 2026 over the six hours before it, showed 43
`doctor.trace` passes with a median of 24.7 seconds, a maximum of 42.9
seconds and none over 60 seconds.

The tests that pin this behaviour are the `waits` tests in
`tests/doctor/test_traces_read.py`, which cover the budget, the step deadline
and the unread list. The gap agent order, skipped judgement and finding text are
tested in `tests/doctor/test_turn_gap_waits.py`. The test in
`tests/swarm/test_wait_history.py` checks that only agents with a long gap are
read.

#### Reading the trace step time

```bash
D=rig-grade-swarm-doctor
journalctl --user -t agentihooks-swarm --since "-6h" \
  | grep '"event": "swarm_tick_step"' | grep '"phase": "finished"' \
  | grep "\"slug\": \"$D\"" | grep '"step": "doctor.trace"' \
  | sed 's/^[^{]*//' \
  | python3 -c 'import sys, json
w = sorted(json.loads(l)["wall_s"] for l in sys.stdin)
print(len(w), "passes; median", round(w[len(w) // 2], 1), "max", round(w[-1], 1), "over 60 s", sum(x > 60 for x in w))'
agentihooks doctor <watched slug> measure wait-history-partial/<watched slug>
```

The first command prints the `doctor.trace` wall seconds per pass, as in
[Measure the timer step](../pillars/swarm.md#measure-the-timer-step). The
second prints the number of agents left unread in the current scan. When the
scan raises no such finding it prints 0 only if the finding was seen before;
otherwise it fails with `no finding wait-history-partial/<slug> has been seen`.

| Symptom | Read |
|---|---|
| `wait history partial` on every scan, with `doctor.trace` near 55 seconds | the Langfuse reads used up the step before the wait reads began; check `trace reader unavailable` and the Langfuse response time |
| `wait history partial` with `doctor.trace` well under 55 seconds | the wait reads themselves used the 15 seconds; many agents have long gaps, or Redis answers slowly |
| `doctor.trace` above 60 seconds | a Langfuse request or a wait read in flight finished past its budget; read the `trace reader unavailable` failure lines for timeouts |
| a turn gap finding disappears while `wait history partial` is present | its agent was left unread in that scan; it is judged again once its wait history is read |
| `wait history partial` evidence reads `live agents unread` above 0 | every retired agent with a long gap was also left unread and the live agents did not fit; see [Live agents first](#live-agents-first) |

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
finding is skipped while the listing is incomplete. How coverage is counted is in
[Trace coverage](#trace-coverage).

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

### Trace coverage

Coverage is judged over the traces the listing returned in that scan, the
listed window. A swarm with more traces than the listing reaches (20 pages of
100, or fewer when the 45 second historical budget ends first) can still reach
full coverage: traces past where the listing stopped are counted on their own
and never as unread.

Each listed trace falls in one of these groups:

| Group | Meaning | Counted as |
|---|---|---|
| covered | every observation page was read, from Langfuse or the cache | read |
| capped | the read stopped at the observation page cap, 20 pages of 50 (1000 observations) | read in part, apart from unread |
| failed | its own observation read failed | unread; retried after every unread trace |
| unread | no settled cache entry for its current update time, and the budget ended before its read | unread |

Traces past the listing are counted as `unlisted`: Langfuse `totalItems` from
the last listing page read, less the unique trace ids listed. A listing that
read every page has none.

`trace coverage partial` is raised when a listed trace is failed or unread and
no Langfuse read failed that scan; a failed listing or active read raises
`trace reader unavailable` instead. Its number and title count the failed and
unread traces in the listed window. It clears when every listed trace is covered
or capped, whatever `unlisted` reads. Its evidence reads:

```text
observations read for 1975 of 2000 traces
trace listing stopped at its page cap
1430 traces past where the listing stopped not counted
18 traces read up to the observation page cap
tool error, task cost and turn gap findings cover only the read traces
each Doctor scan reads more within its read budget
```

The second line appears whenever the listing stopped early, the third only when
`unlisted` is above zero and the fourth only when a listed trace is capped. Up to
50 `trace <id> unreadable: <failure line>` lines follow the cap lines for failed
traces, as on the reader finding.

A capped trace is cached as capped and not read again while its update time is
unchanged. Tool error and turn gap findings on a capped or partly read trace add
`observations read only up to where the read stopped` to their evidence, and a
task with such a trace is left out of the task cost comparison. While the
listing stops early, a task cost finding adds `comparison set truncated: trace
listing incomplete` and the untraced session finding is not judged.

#### Cache entries from older code

Each cache entry in `<swarm home>/<slug>/doctor-traces/` carries a
`deduplicated` flag: its observations were collected by id, so a page Langfuse
returns twice adds nothing. An entry written before the flag existed is upgraded
in place on its next scan, with no Langfuse read, when no two of its
observations are identical; such an entry equals what the id based read gives.
An entry holding a repeated observation, or one with neither whole, capped nor
failed state, is read again within the budget.

The upgrade fixed the cause of the finding on rig-grade-swarm (issue #4663):
the flag requirement made 1974 of 2000 cached traces read as unread and only
about 30 were read again per ten minute scan, while the listing stopped at 2000
of 3430 traces, so the old judgement could never reach full coverage. Measured
with `agentihooks doctor rig-grade-swarm measure
trace-coverage-partial/rig-grade-swarm` it showed 1954 traces unread before the change; on a
copy of the live cache the in place upgrade covered 1975 of 2000 traces in the
first scan.

#### Reading coverage

```bash
agentihooks doctor <slug> measure trace-coverage-partial/<slug>
python -m scripts.doctor.traces_read <slug>
```

The first prints the finding's number, the unread traces in the listed window.
The second runs one pass and prints `measures.coverage.historical` with these
fields:

| Field | Meaning |
|---|---|
| `listed` | `true` when the listing read every page |
| `unlisted` | traces past where the listing stopped |
| `traces` | unique traces listed |
| `covered` | traces with every observation read |
| `capped` | traces read up to the observation page cap |
| `complete` | no listing failure and `covered` plus `capped` equals `traces` |
| `unreadable` | one line per listed trace whose own read failed |

The first reads with the env file keys and refuses any project other than
`agent-swarm` (see [How the Doctor reads traces](#how-the-doctor-reads-traces)).
The second reads with the caller's `LANGFUSE_*` keys; run it where those keys
open the swarm's project.

| Symptom | Read |
|---|---|
| the number falls each scan but stays above zero | the backfill is still reading; each scan reads more within the 45 second budget |
| the number holds after several scans | read `unreadable`: the same traces fail each scan, or Langfuse answers slower than the request timeout |
| `unlisted` is large and `complete` is `true` | expected: the swarm has more traces than the listing reaches; the finding stays clear |
| `capped` rises | sessions with over 1000 observations; their tool error and turn gap findings name the partial read |

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
over 0.2. Turn latency is not judged: a turn runs from one prompt to the
agent's stop, so its length is the agent's working time, tool calls and CI
waits included, and routinely passes ten minutes on a healthy swarm. Breaches
are grouped by the agent's account into one standing
`langfuse_account_<account>` finding, which names each agent, task, value and
threshold. After a successful read, a finding clears when its account no
longer breaches. The tick prints
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
