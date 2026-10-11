# Swarm v2 fleet dashboard and alert contract (SV2-OBS-05)

The fleet dashboard is a projection of recorded observations. It holds no authority, starts no
work and never acts on an alert. Panels, labels, limits and alert rules live in
`observability/swarm-v2.json`; `scripts.swarm_v2.fleet_dashboard` reads them.

## Sample

One observation of the fleet at `at_ms`, numbered by a rising `seq`.

| Field | Shape | Source |
|---|---|---|
| `pools` | `pool`, `configured_maximum`, `nodes`, `template.cpu_millis`, `template.memory_mib` | Declared ASG ceilings and node templates; `nodes` is the joined, ready count |
| `attempts` | `execution_id`, `seat`, `task`, `phase` (`active` or `pending`), `pool`, `permitted_pools`, `requested`, `pending_since_ms`, `protected`, `idle_since_ms`, `terminal`, `authority` | Execution registry and runtime observation |
| `quota` | `account`, `state` (`waiting` or `allowed`), `since_ms`, `resets_at_ms` | Fleet quota readings |
| `archive` | The SV2-OBS-04 diagnose archive entry: `execution_id`, `seat`, `process`, `activity`, `heartbeat_at_ms`, `watermark`, `advanced_at_ms`, `state`, `observed_at_ms` | `Health.diagnose` |
| `checkpoints` | `execution_id`, `seat`, `taken_at_ms` of the newest checkpoint | Checkpoint manifests |
| `brain.feed_at_ms` | Time of the newest brain feed | Brain adapter |
| `brain.denials` | `denial_id`, `at_ms`, `requester_scope`, `target_brain`, `session_title` | Brain routing refusals |

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

An age equal to its limit is not stale. A denial's `session_title` is shown only to a viewer
whose grant names the denial's `target_brain`; every other viewer reads the withheld marker.
The viewer comes from a grant's `brains` list. A display label, a role name or an actor name
never grants a brain, and a grant without a `brains` list is refused.

## Alerts

| Rule | Holds when | Window |
|---|---|---|
| `protected_idle_pod` | An active attempt carries scale down protection and has been idle since `idle_since_ms` | `for_ms` 600000 |
| `impossible_pending` | A pending attempt's request fits no permitted pool template (`no_permitted_template_fits`), or every fitting permitted pool is at its configured maximum (`permitted_pools_at_configured_maximum`) | `for_ms` 120000 |
| `cross_scope_denial` | A brain query was denied across scopes | `for_ms` 0 |

- At most `limit` alerts per rule per evaluation, longest held first; the rest are counted in
  `suppressed`.
- An alert carries its subject, sanitized facts, owner and action, and `automatic` false. It
  never carries a session title.
- `alert_actionability_review` counts alerts that name an owner, an action and their subject.

## Incidents

`IncidentStore` keeps one incident per fingerprint (rule and subject) under the swarm's
`fleet-incidents` hash and the last recorded `seq` under `fleet-incidents-seq`.

- An evaluation whose `seq` is not above the recorded one is stale and changes nothing, so a
  replay after an alert backend restart never duplicates an incident.
- A cleared condition resolves its incident; a condition that returns opens a new one.
- `deliver` hands each opened incident to the sink once, identified as
  `<fingerprint>:<opened_seq>`; a failing sink leaves it for the next delivery. Delivery only
  notifies.

## Rollback

`AGENTIHOOKS_FLEET_ALERTS=off` stops evaluation, recording and delivery of alerts. The
dashboard, the recorded incidents and worker execution are untouched.
