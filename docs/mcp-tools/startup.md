---
layout: default
title: Server Startup
parent: MCP Tools
nav_order: 1
---

# Server startup and deferred loading

The default stdio server keeps an unused session small by postponing the MCP
protocol library and tool implementation imports in its long lived process.
Initialization and discovery work before a tool is called. The first request
that needs the full SDK loads the normal server and continues the same session.
Tool names, input schemas and results keep their existing contracts.

## Startup and first use

`python -m hooks.mcp` uses stdio unless `MCP_TRANSPORT` selects a network mode.
Its startup path has three stages:

1. Generate the tool catalogue in a short lived child process using the current
   Python interpreter and installed source. The child builds each category from
   its real registrations and serializes its schemas, then exits. Its tool
   allowlist is cleared so the catalogue contains every registered tool.
2. In the parent, apply the configured category and tool filters. Answer
   `initialize`, `notifications/initialized`, `ping`, `tools/list`,
   `resources/list`, `resources/templates/list` and `prompts/list` without
   retaining the SDK or hook dispatcher imports. The resource and prompt lists
   are empty. Discovery list requests follow initialization.
3. On another request, including `tools/call`, load the SDK and build the full
   server. Replay the handshake and triggering request into it, suppress the
   duplicate initialization response, and continue reading the same connection.
   Later requests stay on this full server path.

This defers the full server as a whole; it does not promise a separate import
boundary for every individual tool. Request parsing and error handling also
fall through to the SDK when the lightweight path cannot handle an input.
A client can therefore trigger loading without a successful tool call.

The catalogue is cached within a process. A new server process generates its
own catalogue. Startup still has a temporary child process memory and CPU cost;
the saving is in the unused parent's retained imports. A fixed resident memory
size is not guaranteed. Once the SDK loads, it remains loaded for that process.

## Configuration examples

No new setting enables deferred loading: it is automatic for stdio. The same
filters apply to discovery and to the full server after handoff.

| Variable | Default | Effect |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | Deferred parent loading applies to stdio. `sse` and `streamable-http` build the normal server immediately. |
| `MCP_CATEGORIES` | `all` | Comma separated categories: `channels`, `conditions`, `enforcement`, `scheduler`. |
| `ALLOWED_TOOLS` | unset | Comma separated tool names within the selected categories. An unset or blank value permits all selected tools; matching ignores case. |

Run a channels only stdio server:

```bash
MCP_TRANSPORT=stdio MCP_CATEGORIES=channels python -m hooks.mcp
```

Restrict that server to one read operation:

```bash
MCP_TRANSPORT=stdio MCP_CATEGORIES=channels \
  ALLOWED_TOOLS=channel_list python -m hooks.mcp
```

These commands wait for MCP input on stdin. In normal use, the harness starts
and connects to the server registered by `agentihooks init`. Put per session
stdio filters in the harness's server environment configuration and reconnect
or restart that server to apply them. Changing the environment after discovery
does not update an already running connection's advertised catalogue.
For shared network setup and restart commands, see
[MCP Transport](../hooks/mcp-transport.md). Network mode shares one configured
category set among its clients and does not use this deferred stdio path.

## Protocol usage

An MCP client sends one JSON object per line. This exchange initializes the
connection, lists tools and calls the read only `channel_list` tool:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"startup-example","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"channel_list","arguments":{}}}
```

Keep stdin open while reading responses. Initialization and listing produce
responses for request ids `1` and `2`; the notification has no response. The call
at id `3` enters the full SDK path and returns the tool's normal result, without
an extra response for id `1`. Listing alone advertises tools without calling
their implementations. The tools available depend on the configured filters;
use the returned schemas for calls rather than assuming a fixed tool count.

## Limits and troubleshooting

| Observation | Check or action |
|---|---|
| Startup exits before initialization | Read stderr. Catalogue generation must complete successfully within its thirty second timeout; a dependency, registration or child process failure prevents startup. Use the intended installed Python environment. |
| Some tools are absent | Check `MCP_CATEGORIES` and `ALLOWED_TOOLS` together. An allowlist cannot add tools from an excluded category. Reconnect after changing configuration. |
| No tools are advertised | Unknown categories warn on stderr and are skipped. A selection producing no tools emits a zero tools warning; correct the categories or allowlist. |
| Memory rises on the first call | Expected: the full SDK and tool registrations now load. The optimization targets processes that remain on initialization and discovery. |
| Memory rises before a tool call | Check the client's request sequence. Unsupported lightweight requests and malformed inputs can enter the SDK path; catalogue generation also uses a temporary child. |
| A network server loads the SDK at startup | Expected for `sse` and `streamable-http`; compare equivalent transports when investigating memory. |

## Delivery evidence

[Pull request 4431](https://github.com/the-cloud-clockwork/agentihooks/pull/4431)
introduced deferred stdio startup. Its
[red protocol run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39107649399)
observed the hook dispatcher imported after startup and listing. The
[green protocol run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39108690380)
passed all eighteen protocol cases, including calls to the thirteen tools
registered at that delivery. Current categories can contain more tools; the
catalogue is generated from their registrations.
