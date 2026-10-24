---
title: Codex inbox transport
parent: Reference
nav_order: 17
---

# Codex inbox transport

A Codex swarm execution receives inbox items through one of two transports,
chosen by the swarm setting `codex-inbox-transport`:

| Value | Delivery | Master inbox loop |
|---|---|---|
| `app_server` (stored by `swarm create`) | The supervised `codex app-server` owns the thread; the push pump starts or steers a turn for each item, idle or busy. No pane typing. | Ends its turn when idle; no `wait --inbox`. |
| `legacy` | The tick types one wake prompt into an idle worker pane; the agent reads `agentihooks msg inbox`. | Runs the foreground `wait --inbox` loop. |

`agentihooks swarm init <name>` stores `app_server` for a new swarm. A swarm
created before this default change stores `legacy`, and a stored config without
the setting reads `legacy`; either keeps it until someone sets the value. The
Swarm v2 proof controller creates its proof swarms without the setting, so they
run `legacy` unless the proof sets it. Each launch records its transport on its execution; a setting change
applies to the next launch and never converts a running execution.

```bash
agentihooks swarm <id> status --json      # "codex_inbox_transport" in the config block
agentihooks doctor --target codex         # prints the default and the rollback command
```

## Three inbox paths

These are separate mechanisms. None replaces another.

| Path | Who receives | What it is |
|---|---|---|
| Inbox channel | Claude sessions | `scripts.inbox.channel`, a Python MCP server loaded with `--dangerously-load-development-channels=server:inbox`. It pushes each inbox item into the open Claude session and offers a reply tool. |
| Codex push pump | Codex `app_server` executions | `scripts.inbox.codex`, run by the execution's supervisor. It delivers through the app-server protocol; replies go through `agentihooks msg reply`. |
| Broadcast topics | Every subscribed session | `channel_publish` and the broadcast file, filtered by `AGENTIHOOKS_BASE_CHANNELS`. Fleet notices, never addressed work. |

The Bun channel plugins that Claude Code supplies (Telegram, Discord and the
like) are a fourth, unrelated thing: AgentiHooks neither loads nor depends on
them. The inbox channel is its own Python server.

`agentihooks swarm <id> status` and the ledger page Agents panel show whether
each agent takes pushed inbox items and whether a native terminal can attach:
see [Transport capabilities](../pillars/swarm.md#transport-capabilities).

## Qualified versions

| Component | Version | Evidence |
|---|---|---|
| Codex CLI | `codex-cli 0.162.1` | `evidence/cxch-08/qualification.json` (environment), `evidence/cxch-01/compatibility.json` (protocol) |
| Claude Code | `2.1.296` | `evidence/cxch-08/qualification.json` |

A Codex release outside this table is unqualified. Requalify with
`python -m scripts.codex_app_server_probe` before moving a swarm onto it.

## Canary order

Step 4 takes effect when this release deploys: from then on `swarm create`
stores `app_server`. Steps 1 to 3 move swarms that store `legacy`, one at a
time, with `agentihooks swarm <id> set codex-inbox-transport=app_server`. Enable one step at a time, and check each step before the next:

1. One worker on a scratch swarm with its own Redis database.
2. One real worker in an authorized swarm.
3. The master.
4. New Codex executions generally: every swarm made by `swarm create` (the default).

At each step, `agentihooks swarm <id> status` shows the agent's transport and
state, and an item sent with `agentihooks msg send <seat> <text>` is answered
from the live thread without a typed wake.

## Failure diagnosis

| Symptom | Meaning and action |
|---|---|
| Launch fails with `transport` | The swarm setting is `legacy` while the launch asked for `app_server`. Set the transport, or let the tick launch on the recorded setting. |
| Execution `degraded` | The pump hit a protocol or Redis failure. It reconnects to the recorded endpoint, resumes the thread and recovers its open receipts; items stay pending until accepted. |
| Items pending on a connected execution | Open the viewer with `agentihooks swarm <id> terminal <execution-id>` and check the thread for an open approval or a stuck turn. |
| A legacy pane got no wake | The pane was busy, held typed text or was inside the quiet window; the wake retries per `AGENTIHOOKS_INBOX_RETRY_WINDOW_S`, then climbs to the master. |

## Roll back

Rollback changes routing for new work only. The command writes the setting to
the swarm config; on a running swarm it then runs one tick, which updates the
inbox indexes and the tick's own records and may launch new work on the new
value. It deletes no accepted item, moves no history
back to pending, releases no live server's quota slot and leaves `dev` alone.

1. Stop new `app_server` launches:

   ```bash
   agentihooks swarm <id> set codex-inbox-transport=legacy
   ```

2. Let each running `app_server` execution finish its task, or move it with
   the controlled handoff. Never turn typed wakes on for a seat a pump may
   still own.
3. Stand down an execution that must stop now. Its pump stops taking items,
   a publication already in flight gets up to 10 seconds to finish and a
   running turn is interrupted; the app-server is not signalled. If either wait
   runs out, the command exits 1, the execution stays fenced from new inbox
   turns, and you run the stand-down again. A delivery receipt still `submitting`
   or `unknown` may have reached the thread. The pump's recovery pass turns
   `submitting` into `unknown`; nothing resolves `unknown` automatically, so open
   the thread with `agentihooks swarm <id> terminal <execution-id>` and check
   whether the item arrived before moving it. Transfer only items with no
   receipt, a `rejected` one, or an `unknown` one the thread shows never arrived,
   through the seat and task rules:

   ```bash
   agentihooks terminate-agent <agent> --stand-down --dry-run
   agentihooks terminate-agent <agent> --stand-down
   ```

4. The next launch in that seat reads the swarm setting, so the successor
   starts on `legacy` with its task, seat memory and handoff. A master can
   reopen its own conversation instead with
   `agentihooks swarm <id> master up --last`.
5. Transport records, receipts and native sessions stay as recovery evidence.
   Remove an endpoint or process artifact only after its owner has ended.
6. The operator verifies, on the live swarm, Claude delivery, a legacy Codex
   worker wake and a legacy Codex master waiting on `wait --inbox`: send each
   seat an item and confirm it is answered.

   ```bash
   agentihooks msg send <claude seat> "rollback check"
   agentihooks msg send <codex worker seat> "rollback check"
   agentihooks msg send master@<id> "rollback check"
   agentihooks swarm <id> status
   ```

See [Managed Codex terminals](codex-managed-terminal.md) for viewers and push
masters, and [Swarm](../pillars/swarm.md#delivered-then-confirmed) for receipt
and redelivery rules.
