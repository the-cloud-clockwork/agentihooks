---
title: Swarm scoped bus credentials
parent: Reference
nav_order: 50
---

# Swarm scoped bus credentials

A hive member's NATS credential reaches the event, work queue, dead letter and state resources of the swarms it
belongs to and nothing of any other swarm. Every subject a scoped member publishes or reads sits under
`swarm.<swarm>.`. The JetStream API subjects that cannot carry that prefix are granted only for stream and consumer
names that carry the swarm id. A member credential of one swarm is refused by the NATS server on another swarm's
queue, dead letter, state and stream subjects, even when those resources exist.

## When a credential is scoped

`agentihooks hive serve` mints each member's NATS credential at `hive join` and at every refresh. The signer record
named by `AGENTIHOOKS_NATS_SIGNER` decides its shape, and the hive endpoint rereads that record on every refresh, so a
changed list applies without restarting `hive serve`:

| Signer record | Credential | `hive.env` |
|---|---|---|
| No `member_swarms` field | The unscoped member grant: legacy execution subjects, the shared dead letter stream and the shared `KV_<bucket>` store of every hive state bucket | No `AGENTIHOOKS_BUS_SCOPED` line |
| `member_swarms` maps the member id to a list of swarms | Publish and subscribe only under each listed swarm's prefix | `AGENTIHOOKS_BUS_SCOPED=1` |
| `member_swarms` maps the member id to an empty list | Publish denied on `>`; subscribe only on the member's own `_INBOX.<user>.>` | `AGENTIHOOKS_BUS_SCOPED=1` |
| `member_swarms` present, member id absent | None. The endpoint answers 403 `the hub record lists no member swarms for hive member <id>; it mints no unscoped bus credential`, checked before it rotates the member's Redis password | Unchanged |

The signer record field is one JSON object:

```json
{
  "member_swarms": {
    "hive-a": ["rig-grade-swarm"],
    "hive-b": ["rig-grade-swarm", "demo"]
  }
}
```

A `file:` signer reads the same object from a `member_swarms` file in its folder. `hive serve` refuses a record whose
`member_swarms` is not an object of lists with `member_swarms must map member ids to lists of swarm names`, and one
that names a swarm that is not a valid subject token with `member_swarms contains an invalid swarm name (<reason>)`. A member
picks up a changed list at its next `agentihooks hive refresh` or `hive join`, which mints a new credential.

The field is written by two commands, both with `AGENTIHOOKS_NATS_SIGNER` naming an OpenBao record under
`agentihooks/hub/`:

- `agentihooks hive scope [--swarm <swarm> ...]` creates the field and lists every existing member with the given
  swarms, or with every swarm in the store when none is given. Members already listed keep their swarms.
- `agentihooks hive invite` lists each new member the same way once the field exists, and marks its invite
  `AGENTIHOOKS_BUS_SCOPED=1`. It never creates the field, so invites issued before `hive scope` stay unscoped. A
  `file:` signer is never written.

`hive join` with an invite marked `AGENTIHOOKS_BUS_SCOPED=1` refuses an unscoped credential with `the hive offered an
unscoped bus credential; ask its hub to list this member's swarms` and writes no `hive.env`. The refresh behind it
has already rotated the member's Redis password, so the Redis URL in the invite or `hive.env` stops authenticating; run
`agentihooks hive scope` on the hub, then `hive join` again with the same invite. That refusal
guards a scoped member against a downgrade; the hub's 403 is the enforcement, since anyone holding the invite file can
edit its marker.

## Settings

| Variable | Set by | Effect |
|---|---|---|
| `AGENTIHOOKS_BUS=nats` | operator or `hive.env` | Selects the NATS backend |
| `AGENTIHOOKS_BUS_URL` | invite (`hive invite --bus-url`) | NATS address the member reaches |
| `AGENTIHOOKS_BUS_CREDENTIALS` | `hive join` | The minted credential, or an `openbao:` reference to it |
| `AGENTIHOOKS_BUS_SCOPED=1` | `hive invite` and `hive join`, when the grant is scoped | Routes every queue, dead letter and event call to the swarm's own streams; state is reached through `for_swarm(<swarm>)`, and the root bus opens no state bucket |
| `AGENTIHOOKS_BUS_BIND=1` | hive members | Binds existing streams and consumers instead of creating them; members hold no stream create grant |

The hub reads the same variables. Set `AGENTIHOOKS_BUS_SCOPED=1` on the hub, without `AGENTIHOOKS_BUS_BIND`, once
any member is scoped: it then creates each swarm's streams and reads the legacy ones beside them. Without it the hub
keeps the legacy streams only.

## Subjects and streams per swarm

Stream names carry the swarm id as the hex of its UTF-8 bytes, so `rig` becomes `726967`:

| Resource | Stream | Subjects | Consumer |
|---|---|---|---|
| Events | `AGENTIHOOKS-<hex>-EVENTS` | `swarm.<swarm>.events.>` | durable per reactor |
| Dead letters | `AGENTIHOOKS-<hex>-DEAD` | `swarm.<swarm>.dead.>` | durable per reader |
| Work queue, one per lane `eng`, `ci`, `plan` | `AGENTIHOOKS-<hex>-h-<queue-digest>` | `swarm.<swarm>.queue.<lane>.>`; offers publish to `swarm.<swarm>.queue.<lane>.work` | durable named `<lane>` |
| State bucket | `AGENTIHOOKS-<hex>-h-<state-digest>` | `swarm.<swarm>.state.<bucket>.<key>`, 64 revisions kept per key | none; reads use direct message get |

New stream and consumer names use letters, digits and hyphens. Queue and state resource components use
`h-` followed by the full SHA-256 digest of `QUEUE_<lane>` or `STATE_<bucket>`. For example,
the state stream for `other` and `spoke-work` is
`AGENTIHOOKS-6f74686572-h-bfdb74b91986464723022e957a6d5e52d6a5eb0070241ac54c0c979b13c398a6`.
Dotted subject separators remain NATS routing syntax. Valid long consumer names keep their identity;
other consumer names use the same reserved digest prefix.

Existing streams with underscore separators and their consumer cursors remain readable. A hub reuses
those stored identities, and scoped grants include both name formats for each owned resource. New global
streams use `AGENTIHOOKS-EVENTS` and `AGENTIHOOKS-DEAD`; existing `AGENTIHOOKS_EVENTS` and
`AGENTIHOOKS_DEAD` streams remain selected while present. Workspace lookups likewise reuse old labels
before creating a normalized label.

Each swarm has a state stream for `spoke-work`, `spoke-pending`, `hive-presence`, `hive-executions`
and `execution-starts`.

Callers keep their logical names. A scoped bus maps `swarm.<swarm>.ready.<lane>` to the lane queue, `dead.<swarm>`
to `swarm.<swarm>.dead.event`, and an event subject such as `swarm.*.execution.>` or
`swarm.<swarm>.execution.started` to `swarm.<swarm>.events.execution.>` or
`swarm.<swarm>.events.execution.started`. A subject naming another swarm is refused before it reaches the server:
`subject 'swarm.other.execution.started' does not belong to swarm 'rig'`. An event whose `swarm` field differs from the
bus swarm is refused on offer and publish: `event swarm 'other' does not match bus swarm 'rig'`.

## What a scoped member is granted

For each listed swarm the credential allows publishing to:

- `swarm.<swarm>.>`
- for each of the swarm's streams above: `$JS.API.STREAM.INFO.<stream>`, `$JS.API.STREAM.MSG.GET.<stream>`,
  `$JS.API.CONSUMER.INFO.<stream>.*`, `$JS.API.CONSUMER.MSG.NEXT.<stream>.*` and `$JS.ACK.<stream>.>`

and subscribing to `swarm.<swarm>.>` and its own `_INBOX.<user>.>`.

Account slot accounting spans every swarm on a hive, so its two buckets, `account-slots` and
`hive-slot-accounts`, belong to the hub and no member credential reaches them. A member takes, renews and
returns an account slot by a request on `slots.<swarm>.<hive>`, where the hive is the one its credential was
minted for: a scoped member's grant names that subject for each listed swarm, an unscoped member's names
`slots.*.<hive>`. The hub answers it from the `hub-slots` queue group and refuses, and
logs, a request whose body names any other hive. A scoped member that opens
any bucket through the root bus without selecting a swarm gets `PermissionError: select a swarm before opening
scoped state`.

Nothing grants stream or consumer creation, the legacy `AGENTIHOOKS_EVENTS` and `AGENTIHOOKS_DEAD` streams, or any
other swarm's stream names.

## Existing swarms through the subject change

Unscoped credentials and scoped credentials work against the same server at once.

- **Events.** The legacy `AGENTIHOOKS_EVENTS` stream now holds `swarm.*.execution.>` and `legacy.swarm.>`; the hub
  updates an existing stream to that subject list at start. An unscoped publisher whose credential allows
  `legacy.swarm.<...>` publishes there; one that does not keeps its original subject. Hub consumers filter on both,
  and an existing durable that filters on the original subject alone is still accepted. In scoped mode the hub fans
  in the legacy stream and every swarm's own events stream, found from the `AGENTIHOOKS-<hex>-EVENTS` streams on the
  server, so existing reactor filters see both.
- **State.** Before a hub handles an event for a swarm, it creates that swarm's state streams. A scoped state stream
  is not created over a legacy `KV_<bucket>` that still holds entries: it fails with `legacy state requires explicit
  key ownership` until that bucket is migrated.
- **Work queues and dead letters.** Retained legacy work is moved by the migration calls below. A member binding a
  scoped queue or state stream whose import is still marked pending fails with `scoped queue migration is
  incomplete` or `scoped state migration is incomplete`.

## Migrating retained work

The hub's root bus, opened with `scoped=True` and provisioning on, moves one swarm's retained data at a time. Quiesce
that swarm's legacy writers and consumers first.

```python
import os

from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy, credentials_from

hub_creds = credentials_from(os.environ)

bus = nats.open_bus(BusConfig(backend="nats", url="nats://hub:4222", scoped=True, credentials=hub_creds))
bus.migrate_state("rig", "spoke-work", ["claim-1", "claim-2"])
bus.migrate_queue("rig", "swarm.rig.ready.eng", QueuePolicy())
bus.migrate_dead("rig")
bus.close()
```

| Call | Moves | Refuses |
|---|---|---|
| `migrate_state(swarm, bucket, keys)` | Every retained revision of the named keys from `KV_<bucket>`; revision numbers continue past the legacy high water mark | a changed key list or revision set (`state migration ownership or source revisions changed`), a target that already has live writes, a key written during the copy (`legacy state changed during migration`) |
| `migrate_queue(swarm, name, policy)` | Every undelivered message of the legacy lane queue, keeping each one's delivery attempt count | work for another swarm or new work past the start point (`legacy queue changed or contains work for another swarm`), work still held by a consumer (`legacy queue still has undelivered work`) |
| `migrate_dead(swarm)` | Every letter of that swarm from `AGENTIHOOKS_DEAD` into its own dead stream | a retained copy that differs from its source |

Each call is safe to repeat: a copy already present is skipped, and the pending mark clears only when the copy
completes. All three calls refuse a bus without provisioning (`migration requires privileged provisioning`) or without
scoping (`scoped migration is not enabled`).

## Check a member's scope

From the member, with the credential `hive join` saved, a foreign subject is refused by the server:

```bash
nats --server "$AGENTIHOOKS_BUS_URL" --creds "$AGENTIHOOKS_BUS_CREDENTIALS" \
  pub swarm.other.queue.eng.work '{}'
# nats: permissions violation for publish to "swarm.other.queue.eng.work"
```

`hive join` saves the credential as `hive.creds` beside `hive.env` and points `AGENTIHOOKS_BUS_CREDENTIALS` at it. The same refusal holds for `swarm.other.dead.<id>`,
`swarm.other.state.<bucket>.<key>` and the JetStream API subjects of another swarm's streams, such as
`$JS.API.STREAM.MSG.GET.AGENTIHOOKS-6f74686572-h-bfdb74b91986464723022e957a6d5e52d6a5eb0070241ac54c0c979b13c398a6`
and the legacy `$JS.API.STREAM.MSG.GET.AGENTIHOOKS_6f74686572_STATE_spoke-work`.
The pull request suite proves this against the pinned
NATS server in the scoped NATS tests, together with a member's own queue, dead letter and state round trip and a
legacy bus working beside a scoped one.

## No shared hive user

Every hive connects with the member credential `hive join` minted for it. The Anton swarm NATS server trusts only
the operator's signing key: its rendered config carries no `accounts`, `authorization`, `no_auth_user` or password,
so a connection without a signed credential is refused. The service users the hub signs with
`agentihooks hive nats-trust` are listed in antoncore `k8s/values-overlays/agentihooks-swarm-nats/users.yaml`:
`swarm-controller`, `swarm-github-webhook` and `swarm-ledger`. The shared `swarm-hive` user, which subscribed to every
`_INBOX.>` reply subject, is no longer among them, so `hive nats-trust` stops signing it. antoncore's
`test_no_shared_hive_user_remains` reads that file and fails when a user named `swarm-hive` returns, or when any user
is granted publish on the spoke execution subject `swarm.*.execution.>`.

A `swarm-hive` credential signed before the user was retired stays valid until it is revoked; removing the user
only stops new signing. To revoke it, list the user under `retired` in the same file with the OpenBao record to
delete, then rerun `agentihooks hive nats-trust`: its key joins the shared NATS revocation hash and its record is
deleted. antoncore lists no `retired` entry for `swarm-hive` yet.

## The grant check

The `lint` job of the agentihooks test workflow runs `scripts.ci_hive_nats_grants` on pull requests into `dev` and
admitted manual dispatches, never in a merge group or on a push. It runs from the protected grader in the
`Hold the hive member NATS grants equal to the antoncore hive user` step. It reads antoncore dev's NATS values and
users files and compares the grants agentihooks renders with the antoncore users:

| Credential rendered by `scripts/hive/nats_grant.py` | antoncore user | Outcome |
|---|---|---|
| `member_claims`, with its `_INBOX.<user>.>` read as `_INBOX.>` | `swarm-hive` | Compared while the user exists; skipped with a notice once it is gone |
| `controller_claims` | `swarm-controller` | Always compared; a missing user fails the check |

Run it from an agentihooks checkout beside an antoncore checkout:

```bash
python -m pip install "pyyaml>=6.0" "cryptography>=42"
python -m scripts.ci_hive_nats_grants --agentihooks . --antoncore ../antoncore
```

With the hive user retired the check prints:

```text
The antoncore swarm-hive user is retired, so hives connect only with minted member credentials.
The controller NATS grants match antoncore: <n> publish and <m> subscribe subjects.
```

and exits 0. A subject granted on one side only prints one `::error::` line naming the side that grants it, for
example `publish allow <subject> is granted only by the antoncore swarm-controller user
(k8s/values-overlays/agentihooks-swarm-nats/values.yaml).`, and exits 1. The
missing hive user is accepted only in operator mode, where the values file has no `accounts` block and users come from
`users.yaml`; a values file that still defines the `SWARM` account without `swarm-hive` fails with
`::error::The swarm NATS grants cannot be read (ValueError: the SWARM account has no swarm-hive user).`

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `select a swarm before opening a scoped queue` | A scoped bus was asked for a queue name that is not `swarm.<swarm>.ready.<lane>` | Pass the full lane queue name or call `for_swarm(<swarm>)` first |
| `queue '<name>' does not belong to swarm '<swarm>'` | The lane is not `eng`, `ci` or `plan`, or the name names another swarm | Use one of the three lanes of the bus swarm |
| `a scoped bus cannot change its swarm` | `for_swarm` was called on a view of a different swarm | Call `for_swarm` on the root bus |
| `permissions violation for publish to "swarm.<swarm>...."` on the member's own swarm | The signer record does not list that swarm for this member | Add it under `member_swarms`, then `agentihooks hive refresh` on the member |
| Member gets `permissions violation` on every subject | `member_swarms` lists this member with no swarms | Add its swarms, then `agentihooks hive refresh` on the member |
| `hive refresh` or `hive join` answers 403 `the hub record lists no member swarms for hive member <id>` | `member_swarms` exists but omits this member id | `agentihooks hive scope` on the hub, or add the member id with its swarms |
| `the hive offered an unscoped bus credential; ask its hub to list this member's swarms` | The invite is marked scoped but the hub record has no `member_swarms` field | Run `agentihooks hive scope` on the hub, then join again |
| `scoped queue migration is incomplete` | The hub has not finished `migrate_queue` for that lane | Finish the migration on the hub, then restart the member |
| The grant check fails with `the SWARM account has no swarm-hive user` | The antoncore values file still defines the `SWARM` account, so the check expects the shared user | Finish the antoncore operator mode change, which removes the `accounts` block, or restore the user |
| The grant check fails with `the SWARM account has no swarm-controller user` | The controller user is missing from antoncore `users.yaml` | Restore `swarm-controller` there; only the hive user may be retired |
