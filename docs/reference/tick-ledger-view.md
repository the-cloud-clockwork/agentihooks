---
title: Tick ledger view
parent: Reference
nav_order: 66
---

# Tick ledger view

Each tick pass keeps a stored view of its swarm ledger and refreshes it with
one export read of what changed since the view's generation. The view holds
the eight top level parts a pass uses: `tasks`, `_meta`, `phases`, `freezes`,
`priorities`, `followups`, `alerts` and `overview` (`view.PARTS`). The ledger
server answers with only those parts, and sends them in full only when the
view has no generation yet or the server no longer holds every change since
that generation. A pass on a large ledger therefore moves the rows that
changed instead of the whole document.

Every tick step reads the ledger document through
`scripts.swarm.ledger_client.pass_state`, which answers from the held view,
and a lint gate refuses any new whole ledger state read in the swarm and tick
packages (see [The whole ledger state gate](#the-whole-ledger-state-gate)).

The view lives in `scripts.tick.view`; the export parts live in the ledger
API's `export` route. The feature map entry is `swarm-tick-delta-reads`.

## When a pass reads the view

`scripts.swarm.cli.run_tick` starts the view once per pass, inline after the
ledger health probe, which reads the ledger's metadata, and before any tick
step reads the ledger:

1. The pass enters `view.holding()`, which clears any view a previous pass in
   the same process left in the context variables, and drops the pass's view
   when the pass ends.
2. The ledger health probe passes. A failed probe ends the pass as before and
   reads no view.
3. `view.reads(ledger)` is true only when the pass's ledger is exactly
   `scripts.swarm.ledger_client.LedgerClient`. A subclass or any other ledger
   object, such as the test fakes, reads no view.
4. `view.start(slug, ledger.service)` refreshes the stored view, holds it
   in the context variable `view.CURRENT` and records its refresh in
   `view.REFRESH` for the rest of the pass.
5. The pass then wraps the ledger in the controller lease fence and runs its
   steps.

`view.CURRENT` holds a `LedgerView` with `generation`, `tasks`, `meta`,
`phases` and `document`, the whole patched state of the eight parts.
`LedgerView.read(domain, key)` returns the task or phase row whose `id` is
`key`, or the metadata entry `key`, or `None`.

## How steps read the ledger

`pass_state(ledger, slug)` is the one call a step makes for the ledger
document:

| Caller | `pass_state` returns |
| --- | --- |
| A pass holding a view of `slug` | `view.document(slug)`: the held view patched with one delta read, which also replaces `view.CURRENT` |
| A pass holding a view of another ledger, such as the peer a freeze watches or an earlier promotion owner | `ledger.state(slug)`, the whole read |
| A pass holding no view (stand-in ledger, a server that refused named parts) | `ledger.state(slug)` |
| A command outside a pass, such as `swarm park`, `swarm restack` or a plan review decision | `ledger.state(slug)` |

Each call during a pass is one delta read, so a step that writes the ledger
and then calls `pass_state` again sees its own write. The pass's own reads
(the document before the steps, after wrapper refresh, relay and metrics, and
in the stopped branch) and the steps that read the document go through
`pass_state`: ledger state, exits sweep, stopped swarm collection, launch
checks, capacity and spawn stall readiness, master stall and recycle, metrics,
promotion watch, recovery resume, freeze watching, and the declared wait
sweep (`run_waits`).

A pass holding a view makes no whole ledger state call:
`test_a_pass_holding_a_view_makes_no_whole_ledger_read` runs a whole tick pass
with a held view and fails on any `state` call.

## The stored view

The view of one ledger is one JSON file:

```
<scheduler runtime folder>/ledger-views/<first 32 hex of sha256("<base> <slug>")>.json
```

`<base>` is the ledger server address the ledger client resolves for the slug,
so two servers holding the same slug keep separate views. The scheduler
runtime folder is `scripts.scheduler.service.runtime_dir()`:
`~/.cache/agentihooks/scheduler` when the Redis endpoint is
`redis://127.0.0.1:6379/0` and the host is not a hive member (local mode, or
no `AGENTIHOOKS_HIVE_ID`); otherwise a subfolder of it named by a hash of the
Redis endpoint and hive, or of the bus identity when the event backend is the
bus.

The file holds the generation and the state, the same format the ledger
client's stored export uses:

```json
{"generation": 4182, "state": {"tasks": [...], "_meta": {...}, "phases": [...], "freezes": [...], "...": "..."}}
```

It is written to a temporary file beside it and renamed into place, so a
reader never sees half a file. A pass writes it only when the generation moved
or a stored view that no longer fit was replaced; an unchanged ledger leaves
the file untouched.

## One refresh

`view.refreshed(path, reader)` runs the refresh:

| Stored view | Request | Reply | Result |
| --- | --- | --- | --- |
| None, or unreadable | `since` 0 | The eight parts in full | Stored as read |
| At generation `g` | `since` `g` | The changes since `g` | Patched onto the stored state, stored at the new generation |
| At generation `g`, beyond the change window | `since` `g` | The eight parts in full | Replaces the stored state |
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
{"since": 4182, "parts": ["tasks", "_meta", "phases", "freezes", "priorities", "followups", "alerts", "overview"]}
```

| Field | Rule |
| --- | --- |
| `since` | Required integer, at least 0 |
| `parts` | Optional. A non-empty list of distinct names from `tasks`, `_meta`, `phases`, `freezes`, `priorities`, `followups`, `alerts`, `overview`. Any other value, an empty list, a repeated name or another field answers 400 |

Without `parts` the export covers every top level part, as before.

The reply carries the current `generation` and one of two bodies:

```json
{"generation": 4190, "changes": {"set": [[["_meta", "rev"], 5]], "drop": [], "lists": {"tasks": {"order": ["[\"id\",\"t1\",0]", "[\"id\",\"t2\",0]"], "items": {"[\"id\",\"t2\",0]": {"id": "t2", "status": "done"}}}}}}
```

A list item's identity is the encoded `["id", <id>, <occurrence>]` for a row
with an `id`, else `["index", <position>, <occurrence>]`.

```json
{"generation": 4190, "data": {"tasks": [...], "_meta": {...}, "phases": [...], "freezes": [...], "...": "..."}}
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
- a generation in the gap has no recorded row changes, for example after a
  document was imported in place of the ledger, which records none;
- a write in the gap changed more than 5000 paths (`PATHS_KEPT` in the
  repository refresh module), so its row changes were not recorded.

`ResourceClient.changes(slug, since, parts)` sends this request and returns the
reply unchanged; the caller patches it with
`scripts.swarm_ledger.api.client.patched`.

## The whole ledger state gate

`python -m scripts.no_whole_ledger` counts whole ledger state reads per file in
`scripts/swarm/` and `scripts/tick/` and refuses any file that holds more than
the base allowlist allows. A whole ledger state read is any `state` attribute
on a ledger client, called or passed on:

| Counted | Not counted |
| --- | --- |
| `ledger.state(slug)`, `self._ledger.state(slug)`, `context.ledger.state(slug)`, `LedgerClient().state(slug)`, `timing.call(ledger.state, slug)` | `pass_state(ledger, slug)`, `config.state`, `panic.state(store)`, `ledger_probe.state(...)`, `ledger.metadata(slug)`, `Other().state(slug)` |

The receiver counts when it is the name `ledger` or `_ledger`, an attribute
named either, or a direct `LedgerClient()` call. Only tracked Python files are
measured.

The allowlist is `tests/WHOLE_LEDGER_ALLOWLIST.json`, a map of file to count
holding only files with at least one read:

```json
{
 "scripts/swarm/cli.py": 7,
 "scripts/swarm/ledger_client.py": 1,
 "scripts/swarm/readiness.py": 1,
 "scripts/swarm/refresh_job.py": 1,
 "scripts/swarm/trace_plan.py": 1
}
```

The remaining entries are command handlers outside the pass, the readiness
export cache, the queue priority refresh job, the trace plan follow up check,
and `pass_state`'s own fallback. No file under `scripts/tick/` is listed.

### Grading

The lint job in the Tests workflow runs it after the other ratchets:

```bash
python -m scripts.no_whole_ledger --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
```

It fails when:

- a head file reads the whole ledger more times than the base allowlist
  records for it (a file absent from the base allowlist allows zero);
- the head's allowlist differs from the head's own measurement, in either
  direction. Removing a read therefore requires lowering or deleting its
  entry in the same change, which is how the list only shrinks.

A base without an allowlist file grades the head against the head's recorded
allowlist. A file that does not parse fails the gate with
`cannot parse <file>`. The output is one summary line and one `::error::` line
per error:

```
6 files read the whole ledger state, 5 on the base allowlist, 2 errors
::error::scripts/swarm/tick.py reads the whole ledger state 1 times; the base allowlist allows 0
::error::tests/WHOLE_LEDGER_ALLOWLIST.json differs from the head's measurement; run python -m scripts.no_whole_ledger --write
```

`scripts.threshold_pins` pins the gate's `SCOPE` as `covers`, so a change may
widen the graded folders but never drop one, and registers
`tests/WHOLE_LEDGER_ALLOWLIST.json` as graded by `scripts.no_whole_ledger`, so
the allowlist file's shrink rule is this gate's own grade.

### Removing a read

1. Replace the call with `pass_state(ledger, slug)` from
   `scripts.swarm.ledger_client` (or read the held `view.CURRENT` directly).
2. Record the new measurement:

   ```bash
   python -m scripts.no_whole_ledger --write
   ```

3. Commit the code and the smaller `tests/WHOLE_LEDGER_ALLOWLIST.json`
   together.

Grading a tree against itself checks that its allowlist matches its code:

```bash
python -m scripts.no_whole_ledger --base . --head .
```

With this change merged it prints
`5 files read the whole ledger state, 5 on the base allowlist, 0 errors`.

Adding a whole ledger read is refused; a step that needs another part of the
ledger adds that part to `view.PARTS` and to the export route's `parts` enum
instead.

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
generation is the ledger's write counter as of the pass's last refresh; it
advances with ledger writes.

## Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| `ledger view of <slug> skipped: the ledger server refused named parts` on every pass | The ledger server runs code older than the export parts. Update it; the pass keeps working without a view meanwhile |
| No `ledger-views` folder | The pass ran with a ledger other than `LedgerClient`, or the health probe failed before the view starts. Check which ledger `run_tick` received |
| Every pass reads the parts in full | More than 256 generations pass between ticks, or the server records no row changes for some writes. Compare the stored generation with the ledger's current one |
| A pass fails before its first step with an HTTP or connection error from `export` | The ledger server is unreachable or refused the credentials; the error is not the 400 the view skips |
| A view looks stale | Delete its file. The next pass reads the parts in full and stores them |
| A step's `pass_state` result lacks a top level part | The part is not in `view.PARTS`. Add it there and to the export route's `parts` enum |
| Lint fails with `reads the whole ledger state N times` | The change added a whole ledger read in the swarm or tick package. Read through `pass_state` instead |
| Lint fails with `differs from the head's measurement` | The allowlist was not rerecorded after removing or moving a read. Run `python -m scripts.no_whole_ledger --write` and commit the file |

## Tests

- `tests/tick/test_view.py`: first full read, one delta call patching the
  stored view, an exceeded change window, a patch that does not fit, an
  unchanged generation, `start` reading through the ledger client, the view
  holding every top level part the pass reads, and `pass_state` reading each
  document through one delta read.
- `tests/swarm_ledger/test_export_parts.py`: `parts` on full and change
  exports, events only with `_meta`, a changed root, one cached export per part
  set, request validation, and the client's `changes` call.
- `tests/swarm/test_tick_view_start.py`: the pass starts the view once before
  its first ledger read, and never for a stand-in ledger; a pass holding a
  view makes no whole ledger read; a stopped pass reads its ledger through the
  view.
- `tests/test_no_whole_ledger.py`: what counts as a whole ledger read, the
  shrink only grade against the base, a stale allowlist, and `--write`.
