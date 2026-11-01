---
title: Fencing records
parent: Reference
nav_order: 68
---

# Fencing records

Every write the controller lease fences, such as an execution admission, a launch grant, a task claim or a controller
intent, used to rewrite one JSON document per swarm: the swarm's fencing document in the shared `fencing` state bucket.
Each write sent the whole document to Redis and kept 64 copies of it in the swarm's history list. Since
[pull request 9249](https://github.com/the-cloud-clockwork/agentihooks/pull/9249), a swarm moved onto per record keys
writes only the records a change touched, in one Lua script that checks the controller lease and the revision of every
record involved, then writes all of them or none.

The layout is chosen per swarm:

| Layout | When | Where a fenced write goes |
|---|---|---|
| `records` | The swarm was migrated with `python -m scripts.swarm.records_migration` and the state backend is Redis | One hash per section under the swarm's hash tag, written by the fenced batch script |
| `document` | Every swarm not yet migrated, and every swarm on the `bus` state backend | The whole fencing document, as described in [Current execution reads](../swarm-v2/execution-identity.md#current-execution-reads) |

Callers do not choose. `fencing.read`, `fencing.write` and `lease.fenced` read the swarm's document entry and follow
its `moved_to` marker onto the records.

## The records port

`scripts.tick.ports.records` names the operations both layouts answer:

| Operation | Returns |
|---|---|
| `layout(slug)` | `"records"` for a migrated swarm on Redis, else `""` from the Redis adapter and `"document"` from the whole document adapter |
| `sections(slug, names)` | Each named section as a map from key to `Record(revision, value)` |
| `record(slug, section, key)` | One `Record`, or `None` when the key is absent |
| `fenced_batch(slug, FencedBatch(writes, lease))` | The revision the batch committed at |

A `RecordWrite(kind, section, key, expected, value)` is a `put`, a `delete` or a `check`. `expected` is the revision
the writer read the record at: `0` for a record that was absent, or `BLIND` (`-1`) to skip the check. A `LeaseFence(owner,
epoch, now_ms)` makes the batch conditional on the controller lease; an empty `owner` compares only the epoch and the
expiry.

`fenced_batch` raises `StaleLease` when the lease check fails and `RevisionConflict` when any record is not at its
expected revision. Nothing is written in either case.

Two implementations exist:

- `scripts.tick.adapters.records_redis.RedisRecords`, the per record keys.
- `scripts.swarm.fencing.WholeDocumentRecords`, the same port over the whole document. It checks each expected revision
  against the document's revision, applies the batch and writes the document back whole. A concurrent document write
  surfaces as `RevisionConflict`. It serves bus deployments and unmigrated swarms.

`fencing.records(store, slug)` returns the Redis adapter for a migrated swarm and the whole document adapter
otherwise. No tick step calls it yet; fenced writes reach the port through `fencing.write`.

## Redis keys

Every key of a migrated swarm carries the swarm slug as its hash tag, so one script call can read and write all of them.
`AGENTIHOOKS_SWARM_KEY_PREFIX` replaces `agentihooks`:

| Key | Type | Holds |
|---|---|---|
| `agentihooks:swarm:{<slug>}:fence:meta` | hash | The scalars `lease`, `legacy_owner`, `epoch`, `leader_changes`, `grants_disabled`, and `layout` set to `"records"` |
| `agentihooks:swarm:{<slug>}:fence:<section>` | hash | One field per record for each section: `executions`, `archive`, `intents`, `orphans`, `grants`, `registrations`, `claims`, `journals`, `fences`, `operations` |
| `agentihooks:swarm:{<slug>}:fence:by-name` | hash | Agent name to the execution id with the highest generation, derived from `executions` on every write |
| `agentihooks:swarm:{<slug>}:fence:revision` | counter | The swarm's record revision, raised once per committed batch |
| `agentihooks:swarm:{<slug>}:fence:log` | stream | One entry per committed batch with `revision`, `owner` and `records` (the batch size), capped at 1000 entries (`LOG_CAP`) |

Each hash field holds `{"r": <revision>, "v": <value>}`: the revision of the batch that last wrote it and the
record's value. Every record a batch puts gets that batch's revision.

The fence log stream replaces the 64 document copies that the history list kept per write. The document bucket keys
stay in place; see [Fencing state after the move](#fencing-state-after-the-move).

## The fenced batch

`RedisRecords.fenced_batch` runs one Lua script. Inside that script:

1. When the batch carries a lease fence, it reads `lease` from the meta hash and refuses with `FENCE_STALE` when there
   is no lease, the owner differs (only when the fence names one), the epoch differs, or `expires_at` is at or before
   the fence's `now_ms`.
2. For every write whose `expected` is not `BLIND`, it compares the field's stored revision, or `0` for an absent
   field, and refuses with `FENCE_CONFLICT <key>` on the first mismatch.
3. It raises the swarm's record revision, applies every `put` and `delete` (a `check` writes nothing), stamps the
   revision into the `<slug>:records` field of the `fencing` bucket's `current` hash, and appends one entry to the
   fence log stream.

Redis runs the script atomically, so a refusal at step 1 or 2 leaves every key unchanged. The adapter maps
`FENCE_STALE` to `StaleLease` and `FENCE_CONFLICT` to `RevisionConflict`, and raises any other script error unchanged.

The stamp in step 3 makes a records write abort any transaction that watches the `fencing` bucket's `current` hash,
the same way a document write does.

## How a fenced write becomes a batch

`fencing.write(store, slug, change, fence)` on a migrated swarm:

1. Reads `meta`, every section except `archive`, and `by-name` in one pipeline, and assembles them into a document
   shaped like the old one. The assembled document carries a `records_slug` field naming the swarm.
2. Runs `change` on that document. Each section is handed over as a dict that remembers which keys the change read,
   and whether it iterated, counted, compared or copied the whole section.
3. Compares the result with what was read. Every changed or new key becomes a `put` and every removed key a `delete`,
   each expecting the revision it was read at. The `by-name` index is recomputed and written the same way.
4. Adds a `check` for every record the change read but did not write. A section read whole adds a check for every
   record it held when read. When the write is fenced, the `lease` record is left to the lease fence and gets no check.
5. Sends the batch. A change that wrote nothing sends no batch and returns its result.

The `archive` section is not loaded. `fencing.execution_row` reads one archived row by key. `fencing.replace` is the
one change that loads the archive: its archive puts and deletes expect the revisions it loaded. Every other archive
write, such as `ExecutionRegistry.archive` moving superseded executions, puts rows with `BLIND` and no check. Archive
rows a change read but did not write are never checked.

Outcomes:

| Script answer | `fencing.write` does |
|---|---|
| Committed | Returns the change's result |
| `RevisionConflict` | Reads again and reruns `change`, up to 16 attempts, then raises `SwarmError("the swarm fencing state kept changing; nothing was committed")` |
| `StaleLease` | Raises `SwarmError("the controller lease is stale")` with no retry |

`lease.fenced(store, slug, held, change)` passes `held` as the fence. A `Lease` fences on its owner, epoch and expiry; a
bare epoch, as from `lease.fencing(epoch)`, fences on the epoch and expiry only. The lease is still checked inside
`change` as before, so a stale controller is refused both when its change runs and again at the write.

Two writers on different records both commit. Two writers on the same record, or one whose read the other wrote,
commit one after the other: the second gets `RevisionConflict`, reads again and reruns its change.

## Migrating a swarm

The move is one command per swarm, run once every live process that reads or writes fencing state runs a release that
carries pull request 9249. Readers on older code read the document and do not follow `moved_to`, so they would read
stale state after the move.

```bash
python -m scripts.swarm.records_migration rig-grade-swarm --writers-upgraded
```

- Without `--writers-upgraded` the command exits with `upgrade every live fencing reader and writer before migrating
  to per record keys` and writes nothing.
- On the `bus` state backend `fencing.migrate_records` refuses with `per record fencing keys need the Redis state
  backend`.
- The command connects through `scripts.swarm.store.connect`, so it reads the same Redis settings as the tick,
  `AGENTIHOOKS_SWARM_REDIS_URL` included.
- It prints the number of records it wrote, the `layout` marker included. A swarm already migrated prints `0` and
  writes nothing, so the command is safe to repeat.

In one Redis transaction the migration writes every record of the document, external archive included, through the
fenced batch, sets `layout` to `records`, and rewrites the document with `moved_to: "records"`. The transaction
watches the document bucket and the record revision; when either changes before it commits, it plans again, up to 16
times, then raises the contention error above. A document write that read the document before the move fails its own
watch, reads the document again, sees `moved_to` and lands on the records.

### Fencing state after the move

- The document stays in the bucket's `current` hash with its old contents and the `moved_to` marker, and is no longer
  written. `fencing.read` and `fencing.write` still read that entry to find the marker, and then read the records.
- `fencing.read_executions` returns the swarm's executions from the records: the move raises the bucket revision
  without publishing a copy, so the [published copy](../swarm-v2/execution-identity.md#how-the-copy-stays-consistent)
  is no longer used for that swarm.
- `fencing.read_lease`, the lease cache that `lease.current` and `lease.renew` try first, returns nothing for a
  migrated swarm because the cached revision no longer matches the bucket's, so both read the lease from the records.
- `fencing.replace` restores a saved document onto the records.

There is no command that moves a swarm back to the document layout.

## Checking a swarm

Read only:

```bash
P="${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}:swarm"
SLUG=rig-grade-swarm
URL="$AGENTIHOOKS_SWARM_REDIS_URL"
redis-cli -u "$URL" HGET "$P:{$SLUG}:fence:meta" layout
redis-cli -u "$URL" GET "$P:{$SLUG}:fence:revision"
redis-cli -u "$URL" HGET "$P:state:fencing:current" "$SLUG:records"
redis-cli -u "$URL" HLEN "$P:{$SLUG}:fence:executions"
redis-cli -u "$URL" XLEN "$P:{$SLUG}:fence:log"
redis-cli -u "$URL" XREVRANGE "$P:{$SLUG}:fence:log" + - COUNT 5
```

On a hive outside local mode, use `AGENTIHOOKS_HIVE_REDIS_URL` when it is set. A migrated swarm answers
`{"r":<n>,"v":"records"}` for `layout`, and the record revision equals the `<slug>:records` stamp. An empty `layout`
means the swarm still writes the whole document. The newest log entries name the controller that wrote them; an
entry with an empty `owner` came from an unfenced write or a bare epoch fence.

Never write these keys by hand; a fenced write from the tick keeps them consistent.

## Measured size of one change

The PR's byte measurement ran on a copy of the live swarm's fencing document. One agent change sent 2,013,213 bytes
to Redis on the document path and 6,758 bytes on the records path. The figures are recorded in the
pull request's changelog fragment, `changelog.d/engineer-323133-4900.md`.

## Limitations

- A change that iterates a whole section, such as a scan of every execution, adds a check for every record in that
  section, so its batch grows with the section.
- Archive rows are revision checked only when `fencing.replace` writes them.
- Reads of a migrated swarm still load the old document entry to find `moved_to`.
- `python -m scripts.swarm.redis_commands` does not list the `fence:` keys; see the
  [Redis command map](redis-command-map.md).

## Tests

`tests/swarm/test_fencing_records.py` covers the behavior, among others:

| Test | Checks |
|---|---|
| `test_a_stale_controller_racing_a_takeover_is_refused_at_the_write` | A controller whose lease is taken over while its change runs is refused, its write is absent, and the new controller writes |
| `test_a_pass_on_records_writes_no_whole_document` | Fenced writes, an archive move, an authority read and a lease renewal on a migrated swarm leave the document unchanged |
| `test_the_fenced_batch_checks_the_lease_and_writes_all_or_nothing` | A stale lease or one wrong revision leaves every record unchanged |
| `test_the_fence_log_is_a_capped_stream` | The log keeps at most `LOG_CAP` entries |
| `test_every_record_of_a_swarm_shares_its_hash_tag` | Every key carries the swarm's hash tag |
| `test_migration_copies_the_document_into_records_and_reads_it_back` | The move preserves what `fencing.read` returns, and a second move writes nothing |
| `test_a_document_write_in_flight_across_the_move_lands_on_the_records` | A write that read the document before the move retries onto the records |
| `test_the_migration_command_needs_the_writer_rollout` | The command refuses without `--writers-upgraded` |
| `test_the_whole_document_port_fences_and_applies_a_batch` | The whole document adapter checks the fence and revisions and applies the batch |
| `test_a_section_the_change_scanned_is_checked_whole` | A whole section read checks every record in it |

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `SwarmError: the controller lease is stale` from a fenced write | Another controller took the lease, the epoch moved, or the lease expired before the batch ran | Expected for a superseded controller; the live controller's next pass writes. Read the newest fence log entries to see which owner wrote last |
| `SwarmError: the swarm fencing state kept changing; nothing was committed` | Sixteen attempts each met a record another writer changed, often a change that scans a whole section while other writes land | Find the scanning change in the tick journal; the next pass retries |
| The migration exits with `upgrade every live fencing reader and writer before migrating to per record keys` | `--writers-upgraded` was not passed | Confirm every tick, hive and agent process runs a release carrying pull request 9249, then pass the flag |
| `per record fencing keys need the Redis state backend` | `AGENTIHOOKS_STATE_BACKEND` is `bus` | Bus deployments keep the document layout; no migration applies |
| The migration prints `0` | The swarm is already migrated | Nothing to do; check `layout` as above |
| A process shows fencing state that stopped changing after the move | It runs code from before pull request 9249 and reads the document | Restart it on the current release |
| A `ResponseError` other than `FENCE_STALE` or `FENCE_CONFLICT` | Redis refused the script, for example a script cache or permission error | The adapter re-raises it unchanged; read the message and the Redis user's permissions |
