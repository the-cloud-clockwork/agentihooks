---
title: Tick ledger view
parent: Reference
nav_order: 66
---

# Tick ledger view

Each tick pass keeps a stored view of its swarm ledger's tasks, metadata and
phases, and refreshes it with one export read of what changed since the
view's generation. The ledger server answers with only those three parts, and
sends them in full only when it no longer holds every change since that
generation. A pass on a large ledger therefore moves the rows that changed
instead of the whole document.

The view lives in `scripts.tick.view`; the export parts live in the ledger
API's `export` route. The feature map entry is `swarm-tick-delta-reads`.

## When a pass reads the view

`scripts.swarm.cli.run_tick` starts the view once per pass, in the step that
runs before the pass's first ledger read:

1. The ledger health probe passes. A failed probe ends the pass as before and
   reads no view.
2. `view.reads(ledger)` is true only when the pass's ledger is exactly
   `scripts.swarm.ledger_client.LedgerClient`. A subclass or any other ledger
   object, such as the test fakes, reads no view.
3. `view.start(slug, ledger.service)` refreshes the stored view and holds it
   in the context variable `view.CURRENT` for the rest of the pass.
4. The pass then wraps the ledger in the controller lease fence and runs its
   steps.

`view.CURRENT` holds a `LedgerView` with `generation`, `tasks`, `meta` and
`phases`. `LedgerView.read(domain, key)` returns the task or phase row whose
`id` is `key`, or the metadata entry `key`, or `None`. No tick step reads
`view.CURRENT` yet; the steps still read the ledger through the ledger client.

## The stored view

The view of one ledger is one JSON file:

```
<scheduler runtime folder>/ledger-views/<first 32 hex of sha256("<base> <slug>")>.json
```

`<base>` is the ledger server address the ledger client resolves for the slug,
so two servers holding the same slug keep separate views. The scheduler
runtime folder is `scripts.scheduler.service.runtime_dir()`:
`~/.cache/agentihooks/scheduler` for a workstation on the local Redis at
`127.0.0.1:6379/0`, otherwise a subfolder of it named by a hash of the Redis
endpoint and hive, or of the bus identity when the event backend is the bus.

The file holds the generation and the state, the same format the ledger
client's stored export uses:

```json
{"generation": 4182, "state": {"tasks": [...], "_meta": {...}, "phases": [...]}}
```

It is written to a temporary file beside it and renamed into place, so a
reader never sees half a file. A pass writes it only when the generation moved
or a stored view that no longer fit was replaced; an unchanged ledger leaves
the file untouched.

## One refresh

`view.refreshed(path, reader)` runs the refresh:

| Stored view | Request | Reply | Result |
| --- | --- | --- | --- |
| None, or unreadable | `since` 0 | The three parts in full | Stored as read |
| At generation `g` | `since` `g` | The changes since `g` | Patched onto the stored state, stored at the new generation |
| At generation `g`, beyond the change window | `since` `g` | The three parts in full | Replaces the stored state |
| At generation `g`, patch does not apply | `since` `g`, then `since` 0 | Changes, then the parts in full | The full read replaces the stored state |

A patch does not apply when the stored state lacks a key or list item the
changes name, for example after the file was edited or copied from another
ledger. Its `KeyError`, `IndexError`, `TypeError` or `AttributeError` triggers
the one full read, which is always stored, even at the same generation.

`view.start` reads through a `ResourceClient` with the ledger client's base,
credentials (service credentials when the pass runs as the service) and request
timeout. When the server answers HTTP 400, as a server that predates `parts`
does, the pass prints

```
ledger view of <slug> skipped: the ledger server refused named parts
```

to stderr, holds no view and continues. Any other HTTP error, or a connection
error, propagates and ends the pass.

## The export API with parts

`POST /api/v1/ledgers/<slug>/export` with a `since` body reads the export as
changes. The body now also takes `parts`:

```json
{"since": 4182, "parts": ["tasks", "_meta", "phases"]}
```

| Field | Rule |
| --- | --- |
| `since` | Required integer, at least 0 |
| `parts` | Optional. A non-empty list of distinct names from `tasks`, `_meta`, `phases`. Any other value, an empty list, a repeated name or another field answers 400 |

Without `parts` the export covers every top level part, as before.

The reply carries the current `generation` and one of two bodies:

```json
{"generation": 4190, "changes": {"set": [[["_meta", "rev"], 5]], "drop": [], "lists": {"tasks": {"order": ["[\"id\",\"t1\",0]", "[\"id\",\"t2\",0]"], "items": {"[\"id\",\"t2\",0]": {"id": "t2", "status": "done"}}}}}}
```

A list item's identity is the encoded `["id", <id>, <occurrence>]` for a row
with an `id`, else `["index", <position>, <occurrence>]`.

```json
{"generation": 4190, "data": {"tasks": [...], "_meta": {...}, "phases": [...]}}
```

- `changes` holds only rows under the named parts: dictionary entries set or
  dropped, and for each changed list its item identities in order with every
  changed item. Metadata `events` count as changed on every read, so they come
  back only when `_meta` is named.
- `data` holds only the named parts. The server builds and caches one full
  export per part set, so the tick's part set does not displace the full
  export other readers use.
- The metadata entries `seeds` and `api_operations` are never exported.

The server answers in full (`data`) when:

- `since` is 0 or greater than the current generation;
- more than 256 generations (`CHANGES_KEPT` in the SQLite repository) passed
  since `since`;
- any generation in the gap has no recorded row changes;
- a change replaced the whole document.

`ResourceClient.changes(slug, since, parts)` sends this request and returns the
reply unchanged; the caller patches it with
`scripts.swarm_ledger.api.client.patched`.

## Checking it

Read the stored views a hive or workstation holds:

```bash
python - <<'EOF'
from scripts.scheduler.service import runtime_dir
from scripts.swarm_ledger.api.client import read_export_file

for path in sorted((runtime_dir() / "ledger-views").glob("*.json")):
    stored = read_export_file(path)
    print(path.name, "unreadable" if stored is None else (stored[0], len(stored[1].get("tasks", []))))
EOF
```

Each line prints the file name, the view's generation and its task count. The
generation moves with each pass that saw a ledger write.

## Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| `ledger view of <slug> skipped: the ledger server refused named parts` on every pass | The ledger server runs code older than the export parts. Update it; the pass keeps working without a view meanwhile |
| No `ledger-views` folder | The pass ran with a ledger other than `LedgerClient`, or the health probe failed before the view starts. Check which ledger `run_tick` received |
| Every pass reads the parts in full | More than 256 generations pass between ticks, or the server records no row changes for some writes. Compare the stored generation with the ledger's current one |
| A pass fails before its first step with an HTTP or connection error from `export` | The ledger server is unreachable or refused the credentials; the error is not the 400 the view skips |
| A view looks stale | Delete its file. The next pass reads the parts in full and stores them |

## Tests

- `tests/tick/test_view.py`: first full read, one delta call patching the
  stored view, an exceeded change window, a patch that does not fit, an
  unchanged generation, and `start` reading through the ledger client.
- `tests/swarm_ledger/test_export_parts.py`: `parts` on full and change
  exports, events only with `_meta`, a changed root, one cached export per part
  set, request validation, and the client's `changes` call.
- `tests/swarm/test_tick_view_start.py`: the pass starts the view once before
  its first ledger read, and never for a stand in ledger.
