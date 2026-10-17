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
An unsent draft belongs to the viewer: inbox delivery preserves it while the
viewer stays open, but closing and reopening the viewer does not restore it.

## Runtime dependencies

The installed `agentihooks` tool includes `websockets>=15.0` as a runtime
dependency. Managed app-server launches can therefore connect without installing
development extras. The client uses `websockets.sync.client` when it connects;
an installation missing that package fails at connection time, even if importing
the protocol module alone succeeds.

Use the [installed tool setup and import check](../getting-started/installation.md#installed-tool-and-codex-app-server-dependencies)
to verify the CLI's environment. Installing the package in a separate workspace
venv does not repair the uv tool environment.

Worker images install a hashed runtime lock before installing AgentiHooks with
`--no-deps`. The delivered dependency change pins `websockets==17.2` in that
worker lock and updates its recorded checksum. The runtime dependency coverage
check scans imports across the Codex session package and app-server probe,
maps third party imports to distributions, and requires them in both the
runtime dependency set and the worker lock. This keeps future client imports
from silently depending on development-only packages.

The delivery proof for [the runtime dependency fix](https://github.com/the-cloud-clockwork/agentihooks/pull/3552)
verified a fresh tool install without development extras, imported
`websockets.sync.client`, and launched an admitted Codex worker on an isolated
scratch swarm. Its supervisor reached `state=connected` with a thread recorded
and its mounted profile validated. An import check alone does not establish
that a live execution connected or that inbox delivery works.

## Choose the transport

The swarm setting `codex-inbox-transport` accepts `app_server` (what
`swarm create` stores) or `legacy`. A swarm that stored `legacy` keeps it; set it before launching the
Codex agents that should receive inbox work in their thread:

```bash
agentihooks swarm <id> set codex-inbox-transport=app_server
```

The launch records the chosen transport. Changing the setting affects subsequent
executions; it does not convert a running session. To select legacy for
subsequent launches (the full rollback is in
[Codex inbox transport](codex-inbox-transport.md#roll-back)):

```bash
agentihooks swarm <id> set codex-inbox-transport=legacy
```

The swarm launch path passes `--codex-transport app_server` and the admitted
seat to `init-agent`. This flag is for a managed swarm execution, which needs
its swarm identity and admission; it is not a standalone terminal switch.

After the next admitted launch, inspect its recorded transport and state:

```bash
agentihooks swarm <id> status --json
```

Check the selected transport in status, then confirm the supervisor's launch
log reports `state=connected` with its execution, endpoint and thread.
For an end to end inbox check, follow the
[scratch swarm canary order](codex-inbox-transport.md#canary-order) and confirm
an item is answered from that thread. Keep the proof isolated from live work.

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
`codex resume --remote` with the recorded endpoint and thread, with the Codex
update check turned off so a new Codex release never stops it at the update
prompt. It does not create a new conversation. The same command retains the
existing worker-terminal path for Kubernetes executions; see
[Swarm](../pillars/swarm.md).

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

The live qualification also recorded these viewer limits:

- Initial launch can leave its `/rename` command in the composer while the
  server starts work; the recorded thread remains the attachment identity.
- Retiring the execution can leave a reopened viewer showing
  `Reconnecting to server…`. Close that viewer pane separately.
- A Codex update prompt can interrupt attachment. Dismiss or complete it in
  the viewer before checking the conversation history.

| Symptom | Meaning and action |
|---|---|
| Launch fails with `ModuleNotFoundError: No module named 'websockets'` | The launching tool environment lacks its runtime dependency. Update the checkout to a revision containing the dependency fix, reinstall the tool using the installation instructions, and repeat the tool Python import check before retrying an admitted launch. Installing development extras in another venv does not fix this environment. |
| Attach reports `failed` or `exited` | The recorded app-server ended; there is no live thread to attach through that transport. Inspect execution status before attempting recovery. |
| Attach times out without a connected thread | Startup has not produced a connected thread within the timeout. Check the managed server's launch result and serve log; increasing the timeout alone does not repair a failed launch. |
| Reopen reports no connected thread | The immediate reopen command requires an already connected thread. Use the waiting attach helper during startup. |
| Agent runs no attachable app-server | The recorded harness or transport lacks native terminal attachment. Use its normal terminal path. |
| Closing a viewer leaves the agent working | Expected: the viewer does not own execution lifetime or inbox delivery. |

See [Swarm inbox delivery](../pillars/swarm.md#delivered-then-confirmed) for
receipt, redelivery and closure rules, and [Codex compatibility](CODEX-COMPAT.md)
for the hook and profile contract.
