---
layout: default
title: MCP Transport
parent: Hooks
nav_order: 5
---

# MCP Transport — stdio and network modes
{: .no_toc }

1. TOC
{:toc}

---

## Why this exists

Some Claude Code deployments filter **every stdio-transport MCP server** out of
the client at load time. The server is never spawned, its tools never appear,
and `claude mcp add` refuses new registrations regardless of transport. On such
a machine `agentihooks` is simply absent, taking Fleet Command, enforcement and
the brain tools with it.

Network transports (`sse`, `streamable-http`) are not filtered, so `agentihooks`
can run as a persistent server the client connects to instead of spawns.

**stdio remains the default.** Nothing below applies unless you opt in.

## The difference in one table

| | stdio (default) | sse / streamable-http |
|---|---|---|
| Who starts the server | Claude Code **spawns** one per session | you run it; Claude Code **connects** |
| Processes | one per session | one, shared by every session |
| Caller identity | the process env — one process *is* one session | must be passed explicitly |
| If nothing is running | Claude Code starts it | no tools |

That last row is the whole reason a network mode needs a persistent server:
there is no "connect and it starts for you."

## Turning it on

```bash
echo 'MCP_TRANSPORT=streamable-http' >> ~/.agentihooks/.env
agentihooks init
```

That is the whole procedure. `agentihooks init` reads the same
`~/.agentihooks/.env` the daemon does, so one edit drives the `~/.claude.json`
entry, the systemd unit, and the running process together.

**Init always attempts a restart**, and reports the outcome. It has just
rewritten the unit, the url and possibly the port, and a running process carries
none of that, so the restart is unconditional — but success is not guaranteed. A
daemon that exits on startup is reported as `[WARN] Daemon did not start`, with
the reason and `agentihooks mcp start` to retry.

Two costs worth knowing:

- Every `agentihooks init` drops each live agentihooks connection for about a
  second, including a re-run that changed nothing.
- **This is machine-wide.** The install is global, so it interrupts every Claude
  Code session on the box, not only the project you ran it from.

Reverting is symmetric: remove the line (or set `MCP_TRANSPORT=stdio`) and
re-run `agentihooks init`. The stdio entry is restored, the daemon is stopped and
the unit removed, so a downgrade cannot leave a process serving a port nothing
points at.

### Which transport

Prefer `streamable-http`. `sse` is the legacy MCP transport and is being phased
out of the spec; it is supported here because it is the mode most likely to be
verified working behind a restrictive client.

Note the naming mismatch, which is easy to get wrong: the SDK calls it
`streamable-http`, but Claude Code's config schema calls it `http`. The
installer emits the right one.

| `MCP_TRANSPORT` | `~/.claude.json` entry |
|---|---|
| `sse` | `{"type": "sse", "url": "http://localhost:8642/sse"}` |
| `streamable-http` | `{"type": "http", "url": "http://localhost:8642/mcp"}` |

### Spell the host `localhost`

The default host is `localhost`, not `127.0.0.1`, and that is load-bearing under
an enterprise policy.

Observed on a policy-filtered machine: an entry whose url named `127.0.0.1` was
dropped from the client's configured-server set outright — it did not appear in
`claude mcp list`, and `claude mcp get agentihooks` answered *no MCP server
named "agentihooks"*. Changing only the host spelling to `localhost`, with the
same daemon on the same port serving the same transport, connected immediately.

Both names address the loopback interface, so nothing about the access boundary
changes. What changes is whether the client considers the server configured at
all, and the failure is silent — it reads as "the server was never installed"
rather than as a policy denial. `MCP_HOST` still overrides the default; setting
it to the dotted quad on such a machine reintroduces the fault.

## Caller identity — the part that changes how you use the tools

Under stdio, Claude Code spawns one server process per session, so the process
environment identifies the caller. Under a network transport one process serves
every session and no environment lookup can tell callers apart.

One tool therefore takes an explicit `session_id`:

- `channel_acknowledge`

SessionStart injects a banner naming the session's own id so the agent can pass
it. Under stdio the argument is optional and the environment is used as a
fallback; under a network transport **the fallback is disabled entirely** and an
omitted argument makes the call fail.

That refusal is deliberate. A daemon inherits `CLAUDE_CODE_SESSION_ID` from
whatever shell started it, so consulting the environment there does not return
"no idea" — it returns a real, unrelated session id and writes another agent's
state. Failing beats acting as the wrong agent.

Set `MCP_SESSION_ID_BANNER_ENABLED=false` to suppress the banner.

## What a shared server costs

- **One category set for everyone.** A url entry carries no per-client `env`
  block, so `MCP_CATEGORIES` in the daemon's `.env` applies to every session
  pointing at it. Per-profile tool subsetting is stdio-only.
- **Shared fate.** A crash takes the tools out for all sessions at once, which
  is why the unit restarts on any exit.
- **No authentication.** Both transports are plain HTTP with no token. The
  loopback bind *is* the boundary: any local process that can reach the port can
  call every tool. On a single-user workstation that is no worse than
  filesystem access to `~/.agentihooks`. On a shared-account host it is a real
  exposure — keep `MCP_HOST` on loopback, and do not widen it without adding a
  gate.

## Driving the daemon by hand

```bash
agentihooks mcp status     # config vs. reality
agentihooks mcp start
agentihooks mcp restart
agentihooks mcp stop
```

`status` is the one worth knowing. It prints the configured transport and
endpoint, what `~/.claude.json` declares, which supervisor is in play, whether
the process is up, whether the port answers — and names every mismatch between
them. Exit codes make it scriptable: **0** running and matching, **1** stopped,
**2** diverged.

```
agentihooks daemon
  configured transport : sse
  configured endpoint  : localhost:9111
  ~/.claude.json       : sse http://localhost:8642/sse
  supervisor           : pidfile
  process              : running (pid 3542456)
  port                 : closed
  DIVERGED:
    - daemon on port 8642, config says 9111
    - localhost:9111 not answering despite a live process
    - ~/.claude.json url 'http://localhost:8642/sse' does not name port 9111
```

Three lines for one edit, because a port change desynchronises three things at
once — the daemon, the probe, and the client entry. Each is separately
actionable, so each is named.

That is the failure this command exists for. Before it, the same state printed
nothing at all: the client pointed at one port, the daemon served another, and
the only symptom was tools that quietly did not appear.

### Supervisors

Two backends, chosen automatically.

| | when | survives reboot |
|---|---|---|
| **systemd** | a user session exists | yes — the unit restarts it |
| **pidfile** | everything else | no |

The pidfile backend covers WSL2 without `systemd=true`, containers and macOS —
anywhere `systemctl --user` answers *Failed to connect to bus: No medium found*.
It runs the daemon as a detached child and records the pid in
`~/.agentihooks/mcp-daemon.pid`, with output in
`~/.agentihooks/logs/mcp-daemon.log`. There is no supervisor behind it, so
**after a reboot you run `agentihooks mcp start` once.** That is the honest limit
of the fallback.

Set `AGENTIHOOKS_MCP_SUPERVISOR=systemd|pidfile` to force one, which is mainly
useful for exercising both paths on a machine that has systemd. An unrecognised
value warns on stderr and falls back to detection.

The log is appended to across restarts and rolled to `mcp-daemon.log.1` once it
passes 5 MB, keeping one previous generation. Nothing else prunes it —
`agentihooks init --force` clears state files but leaves `logs/` alone.

## The systemd unit

Written to `~/.config/systemd/user/agentihooks-mcp.service`, rendered from a
template in the package. Two details are load-bearing:

**The transport is baked in** as `Environment=MCP_TRANSPORT=…`, from the value
`agentihooks init` validated. This is what stops the daemon and `~/.claude.json`
from disagreeing about which protocol is in play. Transport is an install-time
decision — changing it must rewrite the url too — so it belongs to `init`, not
to a runtime edit.

**There is deliberately no `EnvironmentFile=`.** systemd's parser is not a
shell: it does not strip an `export ` prefix, so `export MCP_TRANSPORT=sse`
would set a key literally named `export MCP_TRANSPORT` and leave the real one
unset. The daemon does not need it anyway — `hooks/config.py` parses
`~/.agentihooks/.env` itself at import, handling `export`, quotes and inline
comments. It parses with `os.environ.setdefault`, so anything systemd injected
would take precedence over the correctly-parsed value.

Every other knob (`MCP_CATEGORIES`, `MCP_HOST`, `MCP_PORT`, …) is an edit to
`~/.agentihooks/.env` plus `agentihooks mcp restart` — which works under either
supervisor, unlike `systemctl --user restart`.

Where there is no systemd user session the unit is still written, and simply
never used; `init` says so and starts the daemon under the pidfile backend
instead.

## Verifying it works

Two checks, in this order — they fail differently and that difference is the
diagnosis.

```bash
# 1. is the daemon up, and does it match the config
agentihooks mcp status

# 2. does the client accept the entry
claude mcp list
```

`claude mcp list` is the one that matters. It reports each configured server and
whether it connects, so it separates the three failures that otherwise look
alike:

| What you see | What it means |
|---|---|
| `agentihooks: … - ✔ Connected` | done |
| `agentihooks: … - ✗ Failed to connect` | entry accepted, daemon down or on the wrong port |
| no `agentihooks` line at all | the client is not reading the entry — policy, or a host spelling it rejects |

That third row is the quiet one. `claude mcp get agentihooks` confirms it by
answering *no MCP server named "agentihooks"* even though the entry is sitting
in `~/.claude.json`.

Do not reach for a bare `curl … -d '{"…","method":"tools/list"}'` here. Stateful
streamable-http requires an `initialize` handshake first and hands back an
`mcp-session-id` header, so a lone `tools/list` returns *Bad Request: Missing
session ID*. That error means the server is speaking MCP correctly — it is not
evidence of a fault, and reading it as one sends you debugging the wrong layer.

```bash
journalctl --user -u agentihooks-mcp.service -f   # or: tail -f the nohup log
```

In a live session, `channel_acknowledge("<message id>", session_id="<your id>")`
should return success, and a call with the argument omitted should return
`no session id resolvable` — that error is the guard working, not a fault.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http`. An unrecognised value warns and falls back to stdio. |
| `MCP_HOST` | `localhost` | Bind address, and the host written into the url. Keep the two identical. Widening this off loopback removes the only access boundary. |
| `MCP_SCHEME` | `http` | Scheme written into the `~/.claude.json` url. The daemon serves plaintext — correct for loopback, where TLS buys nothing. Set `https` only when a TLS reverse proxy fronts it. |
| `MCP_PORT` | `8642` | Bind port. |
| `MCP_SSE_PATH` | `/sse` | SSE event-stream path. |
| `MCP_STREAMABLE_HTTP_PATH` | `/mcp` | streamable-http endpoint path. |
| `MCP_STATELESS_HTTP` | `false` | Stateless mode. Only relevant behind a non-sticky reverse proxy. |
| `MCP_SESSION_ID_BANNER_ENABLED` | `true` | Tell the agent its own session id at SessionStart. |
| `AGENTIHOOKS_MCP_TRANSPORT` | -- | Installer-only override of `MCP_TRANSPORT`, for a one-off `agentihooks init` without editing `.env`. |
| `AGENTIHOOKS_MCP_SUPERVISOR` | `auto` | `systemd`, `pidfile`, or `auto`. Forces a backend instead of probing for a user bus. |
