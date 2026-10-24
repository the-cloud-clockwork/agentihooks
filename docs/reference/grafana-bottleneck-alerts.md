---
title: Grafana bottleneck alerts
parent: Reference
nav_order: 27
---

# Grafana bottleneck alerts

The swarm tick reads the swarm bottleneck alerts from Grafana and keeps one
standing `bottleneck-stage/<stage>` health finding per ledger and stage. The
read goes out from the swarm through the LiteLLM gateway's Grafana tools with
the gateway key the swarm already holds. Nothing on the swarm host listens for
Grafana, so no host on the network can raise or clear a finding by posting to
it.

This replaced the push intake (`agentihooks swarm alerts`, the
`agentihooks-alert-intake` user service on port 8796), which took Grafana
webhook posts from any private address. That command, its service and its
`AGENTIHOOKS_ALERT_INTAKE_*` settings are gone.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_KEY_GATEWAY` | unset | The LiteLLM gateway key, sent as `Authorization: Bearer`. Its LiteLLM unit must reach the `grafana` MCP server, whose `get_annotations` tool the gateway advertises as `grafana-get_annotations`. |
| `LANGFUSE_MCP_URL` | unset | The gateway MCP endpoint. The Langfuse summary pass reads through the same endpoint; `agentihooks init` writes it into `~/.agentihooks/.env` from the profile's `env` block when the file does not set it already. |

With either unset the tick reads nothing and leaves every finding as it is.
There is no separate token, secret path or listener to configure. Each request
carries `Host: llm.homeofanton.com` and `x-mcp-servers: grafana`, and TLS names
that host, as the Langfuse reads do.

Check both are set without printing them:

```bash
for v in MCP_KEY_GATEWAY LANGFUSE_MCP_URL; do
  test -n "$(printenv "$v")" && echo "$v set" || echo "$v unset"
done
```

## What the tick reads

Only the scheduled tick reads Grafana: `agentihooks swarm tick`, which the
swarm timer runs, and the distributed controller's tick. The immediate tick that
`swarm <slug> start` or a master launch runs does not. Each scheduled tick, per
ledger:

1. Calls the gateway tool `grafana-get_annotations` with `Type: alert`, `From`
   one day back and `Limit: 1000`, bounded at ten seconds. A refused key, an
   unreachable gateway, a tool error or a timeout changes nothing and the tick
   goes on.
2. Keeps the alert state annotations whose labels carry `swarm=bottleneck`, a
   `ledger` equal to the ticking ledger's slug and a known `stage`.
3. Takes, per stage, the latest annotation by time, then id, and acts on its
   `newState`:

| Latest state | Effect |
|---|---|
| `Alerting`, `Alerting (NoData)`, `Alerting (Error)` | With a numeric `threshold_hours` label and an `hours` value, updates the standing `bottleneck-stage/<stage>` finding to those hours. A new firing, keyed by the annotation's time, adds one to `Fired N times`; reading the same firing on later ticks does not. Without both numbers nothing changes. |
| `Normal` | Clears the stage's finding when the alert read raised it, measured in task hours in the window. A finding the stage detector or the ci budget pass owns stays. |
| Anything else | Nothing. |

Known stages: `seat_wait`, `review`, `merge_queue`, `intent_fix`,
`launch_failure`, `ci_queue`, `quota_hold`, `host_hold`. Build, ci run and
launch are productive work and never raise a finding. `ci_overrun` belongs to
the ci budget pass alone, so an alert for it is ignored.

From an annotation the tick reads only the labels inside the braces of its
text and the `hours` value. The alert title, summary and any other text never
reach the swarm. A standing finding not updated for six hours stops showing.
How a finding is judged, hidden and brought back is in the
[`verdict` command](../pillars/swarm.md#commands) row.

### Example

An alert state annotation of the shape the tick acts on, with illustrative values:

```json
{
  "id": 4182,
  "time": 1791696000000,
  "newState": "Alerting",
  "prevState": "Normal",
  "text": "Swarm stage ci_queue {alertname=Swarm stage ci_queue, grafana_folder=Swarm, ledger=rig-grade-swarm, stage=ci_queue, swarm=bottleneck, threshold_hours=1} - breach=1.000000",
  "data": {"values": {"breach": 1, "hours": 2.5}}
}
```

On the next scheduled tick of `rig-grade-swarm` this keeps
`bottleneck-stage/ci_queue` at 2.5 task hours against a threshold of 1, and the
tick prints `rig-grade-swarm: fired ci_queue on rig-grade-swarm`. A later
annotation for the same rule with `newState` `Normal` clears it and the tick
prints `rig-grade-swarm: cleared ci_queue on rig-grade-swarm`.

## Alert rules

The rules live in antoncore's Grafana provisioning, one rule per stage in the
`swarm-bottlenecks` group of the `Swarm` folder. Each one queries the
`swarm.bottleneck_stages` ClickHouse table for the latest task hours per
ledger and breaches past the stage's threshold. A rule the tick acts on needs:

| Label or value | Source | Example |
|---|---|---|
| `swarm` label | Rule label | `bottleneck` |
| `stage` label | Rule label | `ci_queue` |
| `threshold_hours` label | Rule label | `'1'` |
| `ledger` label | The query's `ledger` column | `rig-grade-swarm` |
| `hours` value | The query's `hours` refId | `2.5` |

```yaml
labels:
  swarm: bottleneck
  stage: ci_queue
  threshold_hours: '1'
```

The tick reads the alert state annotations Grafana writes when a rule changes
state, and uses no contact point. The `swarm-ledger` webhook contact point and
its `swarm=bottleneck` route in the same provisioning file still post to the
removed intake on port 8796; those posts fail and change nothing.

## Upgrading from the push intake

The swarm timer install disables, stops and deletes the
`agentihooks-alert-intake` user service when its unit file is still in
`~/.config/systemd/user/`. `agentihooks swarm <slug> start` runs that install,
and so does bringing up a stopped swarm's master.
`AGENTIHOOKS_ALERT_INTAKE_HOST`, `_PORT`, `_FROM`, `_TOKEN`, `_CERT` and `_KEY`
are no longer read and can be removed from `~/.agentihooks/.env`.

## Verify

```bash
ls ~/.config/systemd/user/agentihooks-alert-intake.service   # No such file or directory
ss -ltn | grep ':8796 '                                      # no output: nothing listens
agentihooks swarm tick                                        # prints "<slug>: fired <stage> on <slug>" for a newly read firing
agentihooks swarm <slug> status                               # lists bottleneck-stage/<stage> among the health findings while its alert fires
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A rule fires in Grafana but no finding shows | `MCP_KEY_GATEWAY` or `LANGFUSE_MCP_URL` unset in the timer's environment; the key's LiteLLM unit lacks the Grafana annotations tool; the rule's `ledger` label is not the ledger's slug; `threshold_hours` or `hours` is missing or not a number; or the stage is `ci_overrun` or a productive stage. |
| A finding stays after the alert resolved | The finding is owned by the stage detector or the ci budget pass, which clear it themselves, or no `Normal` annotation newer than the firing exists in the last day. |
| `Fired N times` rises without new breaches | Each new firing the tick reads, keyed by its annotation time, counts once. A rule flapping between `Alerting` and `Normal` adds one per firing. |
| Port 8796 still answers | The old service survived an install that did not run the timer setup. Run `systemctl --user disable --now agentihooks-alert-intake.service` and remove its unit file from `~/.config/systemd/user/`. |
