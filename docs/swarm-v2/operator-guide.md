# Operator workflow: ledger, remote agents and closed viewers

The ledger page is a projection of the swarm store. Agents run in worker Pods on Anton; no browser tab, terminal view or personal computer holds their state. Every viewer can be closed during execution, and any viewer opened later reads the same running swarm again.

## Workflow

1. Open the ledger page, Swarm tab, and press **start**. The claimed task's agent runs in a worker Pod; its row shows the backend, generation and profile (`kubernetes · gen 1 · standard`).
2. To watch its terminal, use **attach** on the agent row. Attach opens a terminal view through the Anton herdr server; closing the view leaves the agent running. The row controls are buttons in tab order: Tab moves from **message** to **attach**, Enter fires it, and keyboard focus makes the row's controls fully visible. Once the row has no pending control, Tab from **drain** reaches **cancel**, and Enter on it asks the same confirmation a click does.
3. Close every tab, terminal view and the personal computer. The agent keeps working; the worker heartbeat and the hub's terminal checks keep writing to the swarm store.
4. Reopen the ledger page on any device. The agent row shows the current observation (`seen 0m ago`), the Brain context panel shows its task, and an evidence button opens the record range the task was given.

Desktop at 1920 by 1080 and a phone at 390 by 844 show the same row text for the same agent.

## Destructive scope on a phone

A touch screen has no hover, so every row control is always visible on a phone. Each one is labelled with its agent (`cancel <agent>`) and its scope and consequence in its title:

| Control | Scope | Destructive |
|---|---|---|
| attach | terminal view | no |
| pause | one swarm | no |
| drain | one agent | no |
| cancel | one agent | yes |
| force stop | one agent | yes |

A destructive control asks first, naming the agent and its consequence. Dismissing the question sends nothing. The ledger server takes a destructive control only from the operator role; the label on a button grants nothing. A request repeated with the same operation id queues no second command. A tab whose swarm view is older than the agent it shows is refused by the ledger server as stale and changes nothing: the agent's successor keeps working. The refusal names no execution, Pod or identity. To act again, wait for the next swarm view and tap the control again; a refused request is never retried by itself.

## A reconnecting terminal

When a worker's terminal check falls behind its heartbeat, its row shows the terminal as `cached`, `stale` and `behind heartbeat` until a fresh terminal check arrives. When the page cannot read the swarm status, it keeps the last observed state with the note `Last observed state shown` and disables every row control with the title `the swarm view is not current; controls wait for a fresh read`. A cached page grants no execution authority. Once a fresh read arrives, the stale mark clears and the controls return; nothing queued while the page was disconnected is replayed.

## Rollback

Restore the previous ledger page build by reverting its pull request on `dev`; promotion republishes the ledger server image. Worker Pods are not restarted, and the swarm store, its executions, terminal checks and context records are authoritative and are never deleted as caches. To withdraw only the destructive controls, set `AGENTIHOOKS_LIFECYCLE_DESTRUCTIVE=off` on the ledger server.

## Proof

`tests/e2e/swarm_v2/test_operator_workflow.py` runs the three package cases in real Chromium against a fake swarm store and compares each with its committed record under `evidence/SV2-UX-05/`. The evidence is mocked and labelled so; see `evidence/SV2-UX-05/result.json` for its states.
