# Swarm v2 fleet dashboard and alert contract (SV2-OBS-05)

The fleet dashboard is a projection of recorded observations. It holds no authority, starts no
work and never acts on an alert. Panels, labels, limits and alert rules live in
`observability/swarm-v2.json`; `scripts.swarm_v2.fleet_dashboard` reads them.

## Sample

One observation of the fleet at `at_ms`, numbered by a rising `seq`.

| Field | Shape | Source |
|---|---|---|
| `pools` | `pool`, `configured_maximum`, `nodes`, `template.cpu_millis`, `template.memory_mib` | Declared ASG ceilings and node templates; `nodes` is the joined, ready count. A deployed controller's worker pool reading sets `configured_maximum` to the Pod policy `worker_pool.pods` limit and `nodes` to the distinct nodes running live worker Pods, with no template. It is one pool per controller, named by the worker namespace, and the Kubernetes attempts and pending admissions carry that `pool` |
| `attempts` | `execution_id`, `seat`, `task`, `phase` (`active` or `pending`), `pool`, `permitted_pools`, `requested`, `pending_since_ms`, `protected`, `idle_since_ms`, `terminal`, `authority` | Execution registry and runtime observation |
| `quota` | `account`, `state` (`waiting` or `allowed`), `since_ms`, `resets_at_ms` | Fleet quota readings |
| `archive` | The SV2-OBS-04 diagnose archive entry: `execution_id`, `seat`, `process`, `activity`, `heartbeat_at_ms`, `watermark`, `advanced_at_ms`, `state`, `observed_at_ms` | `Health.diagnose` |
| `checkpoints` | `execution_id`, `seat`, `taken_at_ms` of the newest checkpoint | Each pointer the controller artifacts API commits; an active execution with none yet ages from its start; a reading expires seven days after its execution leaves the active set |
| `brain.feed_at_ms` | Time of the newest brain feed | Brain adapter |
| `brain.denials` | `denial_id`, `at_ms`, `requester_scope`, `target_brain`, `session_title` | Brain routing refusals; not measured yet, because the brain does not refuse a cross scope query |

## Panels

| Panel | Series | Meaning |
|---|---|---|
| `attempts` | `active`, `pending` | Admitted attempts running, and admitted attempts waiting for a node |
| `capacity` | `configured_maximum`, `observed_nodes`, `active_tasks`, `pending_requests` per pool | A configured maximum is a declared ceiling, never a live node count; each series carries its own label |
| `quota_wait` | `waiting_accounts`, `longest_wait_ms` | Accounts held by quota and the longest current hold |
| `archive_lag` | `lagging`, `worst_lag_ms` | Executions whose archive watermark has not advanced within `archive_lag_ms` |
| `checkpoint_age` | `stale`, `oldest_age_ms` | Executions whose newest checkpoint is older than `checkpoint_stale_ms` |
| `connectivity` | `terminal`, `authority` | Operator terminal transport and worker execution authority, counted apart; a disconnected terminal is not a dead worker |
| `knowledge` | `feed_age_ms`, `cross_scope_denials`, `stale`, `denials` | Brain feed freshness and the denied cross scope queries |

An age equal to its limit is not stale. A denial's `session_title` and `target_brain` are shown
only to a viewer whose grant names that brain; every other viewer reads the withheld marker.
The viewer comes from a grant's `brains` list. A display label, a role name or an actor name
never grants a brain, and a grant without a `brains` list is refused.

## Alerts

| Rule | Holds when | Window |
|---|---|---|
| `protected_idle_pod` | An active attempt carries scale down protection and has been idle since `idle_since_ms` | `for_ms` 600000 |
| `impossible_pending` | A pending attempt's request fits no permitted pool template (`no_permitted_template_fits`), or every fitting permitted pool is at its configured maximum (`permitted_pools_at_configured_maximum`) | `for_ms` 120000 |

A pool sample reports its node count, not free room on each node. `permitted_pools_at_configured_maximum`
is therefore a proxy: the autoscaler can add no node, and a request still pending after the
window is taken as one the existing nodes cannot place.
| `cross_scope_denial` | A brain query was denied across scopes | `for_ms` 0 |

- At most `limit` alerts per rule per evaluation, longest held first; the rest are counted in
  `suppressed` and listed by fingerprint in `held` with the shown ones.
- An alert carries its subject, sanitized facts, owner and action, and `automatic` false. It
  never carries a session title or a target brain name.
- `alert_actionability_review` counts alerts whose owner and action match their rule, that name
  their subject and never act, and lists the rest as `unactionable`.

## Incidents

`IncidentStore` keeps one incident per fingerprint (rule and subject) under the swarm's
`fleet-incidents` hash and the last recorded `seq` under `fleet-incidents-seq`.

- An evaluation whose `seq` is not above the recorded one is stale and changes nothing, so a
  replay after an alert backend restart never duplicates an incident.
- Only shown alerts open incidents. An open incident resolves only when its fingerprint is no
  longer in `held`, so a suppressed condition never reads as cleared; a condition that returns
  after resolving opens a new incident. A suppressed incident keeps the `facts` and `last_seq` of the last
  evaluation that showed it.
- `deliver` hands each opened incident to the sink once, identified as
  `<fingerprint>:<opened_seq>`; a failing sink leaves it for the next delivery. Delivery only
  notifies. Every store write watches its keys and retries up to five times when another
  writer changed them. Delivery is at least once at one edge: when the notified mark still
  fails after those retries, `deliver` raises after the sink ran and the next delivery sends
  the same `incident_id` again, so a sink deduplicates by it.

## Entrypoint and recorded canary

`python -m scripts.swarm_v2.fleet_view --grant <grant.json>` with either `--samples <trace.json>`
(a JSON list of samples) or `--canary <SV2-CAP-02 result> --overlay <readings.json>` prints, per
trace, the dashboard of its last sample, the alerts held at every sample and the actionability
review. A grant without a `brains` list exits 2 with the refusal on stderr.

`scripts.swarm_v2.fleet_canary` turns each live run of the recorded antoncore SV2-CAP-02 scaling
canary into one sample at the canary Pod's creation and one per recorded stage: the
`anton-k3s-swarm` group with its configured maximum, the node once it is ready, and the Pod
pending until it starts. The overlay joins the quota, archive, checkpoint and brain readings.
The record carries no resource request or node template, and an unknown shape never proves a
request impossible, so only the at maximum test applies to recorded runs.

## Compatibility

- The archive panel reads the SV2-OBS-04 `Health.diagnose` archive entry unchanged.
- The dashboard, alerts and incidents add the keys `fleet-incidents` and `fleet-incidents-seq`
  under each swarm and change no existing key, command or API.
- The local swarm path never reads the definition or the module; with the module absent or alerts
  off, local execution behaves as before.

## Rollback

`AGENTIHOOKS_FLEET_ALERTS=off` stops evaluation, recording and delivery of alerts. The
dashboard, the recorded incidents and worker execution are untouched.

Data safety: the execution registry, quota, archive, checkpoint and brain records are
authoritative and only read. The incident hash and evaluated sequence are operational records
owned by this package; a rollback leaves them in place for audit, and removing them never
touches an authoritative object.
