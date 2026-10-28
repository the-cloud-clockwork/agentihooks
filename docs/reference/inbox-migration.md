# Guarded inbox migration

Run the existing `scripts.inbox.migration.main` command after the events launch
fix has merged, its release is running, and the live events hour has passed.
The master chooses the time; there is no operator approval step.

The migration copies open items, including their ids, states and history. It
does not remove Redis items or change the receiving transport. Copy parity and
delivery through NATS are separate completion checks.

## Connections and evidence

Run from the installed release on the workstation. Create an evidence directory
with `agentihooks scratch new` and set `MIGRATION_DIR` to its returned directory.
Keep snapshot contents private: inbox text and history must not enter command
output. Record only counts, digests and receipt identities on the task.

Supply `COPY_BUS_URL`, `COPY_BUS_CREDENTIALS`, `LIVE_BUS_URL` and
`LIVE_BUS_CREDENTIALS` through the credential manager. The credentials variables
contain credential references accepted by `BusConfig`, never printed values.
The copy must be an isolated NATS destination, with separate state from live.
The migration refuses a receipt from the same URL or destination identity.
Use connections authorized for migration state and every destination seat.
A scoped hive connection that reports `select a swarm before opening scoped
state` cannot run this migration; retain the refusal rather than changing its
credential scope locally.

The commands below run in shells with the corresponding connection configured.
Do not point the copy shell at the live destination.

## Snapshot and rehearse

```bash
python -c 'from scripts.inbox.migration import main; raise SystemExit(main())' \
  snapshot "$MIGRATION_DIR/source.json" > "$MIGRATION_DIR/source-report.json"
cat "$MIGRATION_DIR/source-report.json"
```

In the copy shell:

```bash
AGENTIHOOKS_BUS=nats AGENTIHOOKS_BUS_URL="$COPY_BUS_URL" \
  AGENTIHOOKS_BUS_CREDENTIALS="$COPY_BUS_CREDENTIALS" \
  python -c 'from scripts.inbox.migration import main; raise SystemExit(main())' \
  rehearse "$MIGRATION_DIR/source.json" \
  --receipt "$MIGRATION_DIR/rehearsal.json" \
  > "$MIGRATION_DIR/rehearsal-report.json"
cat "$MIGRATION_DIR/rehearsal-report.json"
```

The report carries item count, history count and record digest. Rehearsal reads
back every imported record and refuses a differing record or duplicate item id.
Retain both reports and the receipt.

## Apply

Once the prerequisites pass, use the live shell:

```bash
AGENTIHOOKS_BUS=nats AGENTIHOOKS_BUS_URL="$LIVE_BUS_URL" \
  AGENTIHOOKS_BUS_CREDENTIALS="$LIVE_BUS_CREDENTIALS" \
  python -c 'from scripts.inbox.migration import main; raise SystemExit(main())' \
  apply "$MIGRATION_DIR/rehearsal.json" --enable-live \
  > "$MIGRATION_DIR/live-report.json"
cat "$MIGRATION_DIR/live-report.json"
```

Apply checks the current source against the rehearsal and checks it again after
the import. A changing source makes cutover unverified. Take a fresh snapshot
and rehearse again against an isolated destination before another apply; do not
edit the receipt to force a match. A failed apply can have imported records;
retain its error and inspect existing records before retrying. Import refuses
an existing id whose saved record differs.

## Read-only parity check

In the live shell, configure the same live bus connection as apply, then run:

```bash
python - "$MIGRATION_DIR/source.json" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

from scripts.events.config import BusConfig
from scripts.events.factory import open_bus
from scripts.inbox.migration import digest, read_records

source = json.loads(Path(sys.argv[1]).read_text())
bus = open_bus(BusConfig.from_environ())
try:
    destination = read_records(source, bus)
finally:
    bus.close()
source_ids = sorted(record["item"]["id"] for record in source)
destination_ids = sorted(record["item"]["id"] for record in destination)
assert source_ids == destination_ids
assert len(destination_ids) == len(set(destination_ids))
print(json.dumps({
    "source_items": len(source),
    "bus_items_verified": len(destination),
    "distinct_ids": len(set(destination_ids)),
    "ids_digest": digest(destination_ids),
    "states": dict(Counter(record["item"]["state"] for record in destination)),
    "records_digest": digest(destination),
}))
PY
```

This checks the imported snapshot's records, including state and history; it
does not count unrelated bus records or prove agent delivery. Reconcile the
source, rehearsal and live report digests before accepting this parity output.

## Live delivery and completion

Use the deployed receiving transport, with NATS selected through its Git-managed
configuration. Send an actual coordination item with `agentihooks msg send` to
the receiving seat. Record its returned id and the receiver's read and reply
commands. Pair those with the receiving transport's NATS receipt evidence for
that same id and seat. A Redis recheck or fallback, or a send warning, is not
evidence of NATS delivery. Do not create a second consumer for a live seat just
to inspect its receipt: it could claim the agent's mail.

Retain the migration and parity outputs, per-id receipt evidence and any
refusals on the task. Close only after the master judges preservation,
idempotence and live NATS delivery. Redis retirement is separate work.
