---
title: Managed Codex terminals
parent: Reference
nav_order: 16
---

# Managed Codex terminals and push masters

A Codex swarm execution using `app_server` runs through a supervised server.
Herdr displays a native Codex terminal attached to that execution's recorded
thread. Closing the terminal closes the viewer; the server and inbox delivery
continue. Reopening the viewer attaches to the same thread.

## Choose the transport

The swarm setting `codex-inbox-transport` accepts `legacy` (the default) or
`app_server`. Set it before launching the Codex agents that should receive
inbox work in their thread:

```bash
agentihooks swarm <id> set codex-inbox-transport=app_server
```

The launch records the chosen transport. Changing the setting affects subsequent
executions; it does not convert a running legacy session. To select legacy for
subsequent launches:

```bash
agentihooks swarm <id> set codex-inbox-transport=legacy
```

The swarm launch path passes `--codex-transport app_server` and the admitted
seat to `init-agent`. This flag is for a managed swarm execution, which needs
its swarm identity and admission; it is not a standalone terminal switch.

Delivery and attachment depend on the recorded harness and transport:

| Execution | Push inbox | Native remote terminal attachment | Foreground inbox wait |
|---|---|---|---|
| Claude | Yes, through its channel | No | No |
| Codex `app_server` | Yes, through its managed transport | Yes | No |
| Codex `legacy` | No | No | Yes for the master's inbox loop |

The inbox waker skips pane typing when the execution's transport owns push
delivery. Master panes are never typed into by that worker wake path. An unsent
draft in the viewer is separate from incoming inbox work.

## Open and reopen a viewer

The managed launch starts the server separately and runs the attach helper in
the foreground of its herdr pane. To reopen a local managed Codex execution,
use its recorded execution identity:

```bash
agentihooks swarm <id> terminal <execution-id>
```

For a connected local execution this opens a herdr tab in the swarm workspace
and prints JSON containing `agent`, `thread` and `pane`. The viewer runs
`codex resume --remote` with the recorded endpoint and thread. It does not create
a new conversation. The same command retains the existing worker-terminal path
for Kubernetes executions; see [Swarm](../pillars/swarm.md).

For attachment diagnostics, the launcher helper can wait for the recorded thread:

```bash
python -m scripts.codex_session attach \
  --slug <id> \
  --agent <registered-agent-name> \
  --timeout 120
```

It polls every half second for a connected transport with a thread id. The
default timeout is 120 seconds. `--codex-bin` selects the Codex executable.
A viewer that exits nonzero in under five seconds is retried, up to three
attempts, because a newly created thread may not yet have a resumable rollout.
A successful exit or a viewer that stayed open at least five seconds returns
its exit status without another attempt.

## Work as a push master

A push-mode Codex master's opening prompt tells it to read `agentihooks msg
inbox`, handle every open item, then end its turn when no work remains. New
items arrive in that thread after the turn finishes. It must not enter
`agentihooks swarm <id> wait --inbox`, including when older profile text asks
for that loop.

Receipt does not settle an action item. Read and answer it through the inbox:

```bash
agentihooks msg inbox
agentihooks msg read <item-id>
agentihooks msg reply <item-id> "The requested work is complete."
```

An explicit dependency still uses the normal swarm wait, for example:

```bash
agentihooks swarm <id> wait --on checks <pull-request-url>
```

Waits on a reply or another task remain available too. A legacy Codex master
retains its foreground `wait --inbox` loop.

Push-mode handoff rendering replaces a Next instruction to rearm a Monitor
with reading the inbox. The transfer's expected first Next action receives the
same rewrite, so continuity confirmation compares the rendered action exactly.
Legacy rendering retains the foreground inbox wait. New push-master handoffs
should make their first Next action read `agentihooks msg inbox`.

## Lifecycle and failures

Reopening a viewer and retiring an execution are separate operations. Managed
execution retirement uses its supervised runtime. The ordinary runtime resume
path refuses a managed execution with `unsupported resume: a managed Codex
execution continues through its supervised server`. Use terminal reopening for
a live execution. Renaming a managed agent renames its herdr pane and registry
identity without typing a session-name command into the viewer.

| Symptom | Meaning and action |
|---|---|
| Attach reports `failed` or `exited` | The recorded app-server ended; there is no live thread to attach through that transport. Inspect execution status before attempting recovery. |
| Attach times out without a connected thread | Startup has not produced a connected thread within the timeout. Check the managed server's launch result and serve log; increasing the timeout alone does not repair a failed launch. |
| Reopen reports no connected thread | The immediate reopen command requires an already connected thread. Use the waiting attach helper during startup. |
| Agent runs no attachable app-server | The recorded harness or transport lacks native terminal attachment. Use its normal terminal path. |
| Closing a viewer leaves the agent working | Expected: the viewer does not own execution lifetime or inbox delivery. |

See [Swarm inbox delivery](../pillars/swarm.md#delivered-then-confirmed) for
receipt, redelivery and closure rules, and [Codex compatibility](CODEX-COMPAT.md)
for the hook and profile contract.
