# Ledger durability, one active writer

SV2-LDG-04 keeps the file based ledger correct without pretending the in-process `ledger_core.LOCK`
coordinates replicas. That lock serializes threads inside one server; it says nothing to another
process. The interface is `scripts.swarm_v2.ledger_writer`.

## The writer and its volume

- One ledger server is the active writer of one ledger folder (`LEDGER_DIR`). Ledger state lives only
  in the SQLite database `ledgers.sqlite3` and its WAL files. The folder also holds the writer lease,
  the page layout file the server replaces atomically, and legacy `<slug>.json` and `<slug>.html`
  files, which the server imports once after a verified backup and never writes again.
- On Anton the folder is the ledger StatefulSet's `data` volume (`ReadWriteOnce`), mounted at `/data`.
  On the workstation it is `~/development-ledger`.
- The ledger stays authoritative for operator work state. Every remote worker and the controller reach
  it over the authenticated HTTP API, never by mounting the folder.

## The writer lease

- `ledger_server.serve` takes `WriterLease(LEDGER_DIR).acquire(owner())` before it adopts legacy
  files, starts its threads or binds its port. The lease is a non-blocking exclusive `flock` on
  `.ledger-writer.lock`, held for the life of the process and recorded as `{pid, host, started_at}`.
- A second server on the same folder exits non-zero at startup with
  `ledger folder <dir> already has an active writer, pid <pid> on <host>; a second writer is refused`,
  before it touches the folder. Each refusal appends one line to `.ledger-writer-conflicts.jsonl`;
  `conflicts_total(dir)` reads it as `ledger_writer_conflicts_total`.
- The kernel drops the lease when the holder exits, crashes or is killed, so a restart needs no cleanup.
  A code reload (`SWARM_RELOAD=1`) re-executes the same process; the lock descriptor closes on exec and
  the new image takes the lease again.
- `flock` coordinates processes that share one kernel's view of the volume: a local disk or a
  `ReadWriteOnce` volume. It does not coordinate writers across NFS or another shared network
  filesystem; never put the ledger folder on one.
- The lease excludes a second writer instance, meaning a second ledger server. Command line tools that
  open the repository on the workstation (`new_ledger`, swarm snapshot restore, storage migration) are
  transactional clients rather than writer instances: SQLite's `BEGIN IMMEDIATE` serializes each of
  their transactions with the server's, and none holds state across transactions. Remote workers never
  open the folder; they write through the API.

## Authority

- The one writer takes the actor and scope of every write from its credential: the ledger token is the
  operator role, and a hive or agent token names a bound agent through `ledger_authority.principal`. A
  display label alone (`X-Ledger-Agent` with no token) is answered 403 `missing or wrong ledger token`
  and changes nothing; T-SV2-LDG-04-A records it.
- The `ledger_writer` command acts with the file permissions of the ledger folder and the writer lease;
  it never reads a label to decide what it may do.

## Chart

`ledger.replicas` in the `agentihooks-swarm` chart must be 1. Any other value fails `helm template`
with `ledger.replicas must be 1`, and the chart proof (`ci/kind-smoke.sh`) checks that refusal. A
replica forced past the chart still meets the lease and exits.

## Snapshots and backups

- `python -m scripts.swarm_v2.ledger_writer --dir <folder> snapshot <target>` copies the live database
  through the SQLite backup API while the server keeps writing. The copy goes to a hidden temporary
  file in the target's folder, passes `PRAGMA integrity_check`, and is then renamed over the target. A
  reader never sees a partial copy; a failed copy leaves the previous target in place. The database is
  the only ledger state to back up.
- `holder` prints the lease record and the conflict count.

## Restart and replay

A restarted writer opens the database and SQLite recovers the last committed transaction from the WAL.
Operations carry ids: a client that replays an accepted operation after a lost reply gets it applied
once (SV2-LDG-01, SV2-LDG-03).

## Rollback

1. Take a snapshot while the current server runs, or pick a verified one.
2. Stop the ledger server (scale the StatefulSet to zero, or stop the workstation server).
3. With the current image, `python -m scripts.swarm_v2.ledger_writer --dir <folder> restore <backup>`
   verifies the backup, takes the writer lease (it is refused while a server holds the folder) and
   writes the backup over the database through the backup API.
4. Deploy the preceding writer image through GitOps and start it. It opens the restored database and
   takes no lease; the chart still runs one replica.
5. The database, its page tokens and the Redis swarm state are authoritative objects; none of them is
   deleted as a cache.

T-SV2-LDG-04-C rehearses this: it restores a verified snapshot over a newer state, starts a writer on
the restored folder, and records that the writer holds the lease and serves the committed state.
