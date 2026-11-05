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
A running pass reads its ledger once, plus at most once after each ledger
write (see
[One ledger read per pass](#one-ledger-read-per-pass)). Each of those reads
is one delta read, or a delta read and a full read when the delta does not
apply (see [One refresh](#one-refresh)).

The view lives in `scripts.tick.view`; the export parts live in the ledger
API's `export` route. The feature map entry is `swarm-tick-delta-reads`.

## When a pass reads the view

`scripts.swarm.cli.run_tick` starts the view once per pass, before any tick
step reads the ledger. A running swarm starts it after the `wrapper probes`
group, which holds the ledger health probe that reads the ledger's metadata:

1. The `wrapper probes` group passes. A failed ledger health probe ends the
   pass as before and reads no view.
2. The pass enters `view.holding()`, which clears any view a previous pass in
   the same process left in the context variables, and drops the pass's view
   when the pass ends.
3. `view.reads(ledger)` is true only when the pass's ledger is exactly
   `scripts.swarm.ledger_client.LedgerClient`. A subclass or any other ledger
   object, such as the test fakes, reads no view.
4. `view.start(slug, ledger.service)` refreshes the stored view, holds it
   in the context variable `view.CURRENT` and records its refresh in
   `view.REFRESH` for the rest of the pass.
5. The pass then wraps the runtime in `controller.FencedRuntime` and runs its
   steps.

A stopped swarm runs no `wrapper probes`: its pass enters `view.holding()` and
starts the view right after the `before` group, then runs the stopped branch.

`view.CURRENT` holds a `LedgerView` with `generation`, `tasks`, `meta`,
`phases` and `document`, the whole patched state of the eight parts.
`LedgerView.read(domain, key)` returns the task or phase row whose `id` is
`key`, or the metadata entry `key`, or `None`.

## How steps read the ledger

`pass_state(ledger, slug)` is the one call a step makes for the ledger
document:

| Caller | `pass_state` returns |
| --- | --- |
| A pass holding a clean view of `slug` | `view.document(slug)`: the held view's document, with no ledger read |
| A pass holding a view of `slug` that a ledger write left stale | `view.document(slug)`: the held view patched with one delta read, which replaces `view.CURRENT` and the held view and clears the stale mark |
| A pass holding a view of another ledger, such as the peer a freeze watches or an earlier promotion owner | `ledger.state(slug)`, the whole read |
| A pass holding no view (stand-in ledger, a server that refused named parts) | `ledger.state(slug)` |
| A command outside a pass, such as `swarm park`, `swarm restack` or a plan review decision | `ledger.state(slug)` |

A call on a clean view costs no ledger read. A step that writes the ledger and
then calls `pass_state` again sees its own write, because the write marks the
held view stale and the next call reads the changes. The pass's own reads
(the document before the steps, after wrapper refresh, relay and metrics, and
in the stopped branch) and the steps that read the document go through
`pass_state`: ledger state, exits sweep, stopped swarm collection, launch
checks, capacity and spawn stall readiness, master stall and recycle, metrics,
promotion watch, recovery resume, freeze watching, and the declared wait
sweep (`run_waits`).

A pass holding a view makes no whole ledger state call:
`test_a_pass_holding_a_view_makes_no_whole_ledger_read` runs a whole tick pass
with a held view and fails on any `state` call.

## One ledger read per pass

A running pass reads its ledger once when it starts the view, then at most
once more after each ledger write, and only when a later step reads the
document. A write followed by no read costs nothing. Every other whole
document read, the capacity step's included, is served from the held view.

### The held view and its stale mark

`view.start` holds the view in `view.CURRENT`, records the slug and refresh
function in `view.REFRESH`, and holds it in the context
variable `view.HELD` as a `view.Held` record with the view, the function that
reads it again (`renew`) and a `stale` flag that starts false.

| Event | Effect |
| --- | --- |
| A ledger write through `LedgerClient` succeeds | `LedgerClient._call` calls `view.wrote()` after any call carrying operations, which sets `stale` on the held record. A refused or failed write raises before the mark, so it leaves the view clean |
| `pass_state(ledger, slug)` or `view.document(slug)` on a clean view | Returns the held view's document. No ledger read |
| `pass_state(ledger, slug)` or `view.document(slug)` on a stale view | One delta read through `renew`; the result replaces the held view and `view.CURRENT`, and `stale` returns to false |
| `view.current()` on a stale view | The same delta read, returning the renewed `LedgerView` |
| `view.document(slug)` for a ledger other than the held one | `None`, so `pass_state` falls back to `ledger.state(slug)` |
| The pass ends | `view.holding()` drops `CURRENT`, `REFRESH` and `HELD` |

`view.wrote()` does nothing when the pass holds no view, so a write outside a
pass, such as a command handler's, changes nothing here.

### What reads no ledger

| Step | Reads |
| --- | --- |
| Planning and phase pass (`cli._phase_steps`) | Hands `phases.phase_changes` the pass document. `phases.phase_changes(doc)` takes only the document and projects each phase's tasks from it with `scripts.swarm.phase_projection.projected_phase_tasks`; it makes no ledger hierarchy call |
| Promotion refresh (`promotion_watch.refresh`) | Uses the `doc` the pass hands it for the owning swarm instead of calling `pass_state` again. Earlier promotion owners are other ledgers, so their red and recovery checks still read them whole through `pass_state` |
| Capacity | Calls `pass_state`, which the clean held view answers with no read |

When the planning pass returns any action, `_phase_steps` calls `pass_state`
once before the phase pass. A planning write has marked the view stale, so
that call reads the changes and the phase pass sees the planning writes; with
no write, the clean view answers it with no read.

### The phase projection

`projected_phase_tasks(doc)` returns a map of phase id to a list of
`(task id, state)` pairs, computed from the document alone with the same node
projection the ledger's hierarchy repository stores:

1. `scripts.swarm_ledger.repository.hierarchy.project(doc)` builds the nodes
   of `plans`, `phases`, `slices` and `tasks`. A task's parent is its `slice`,
   else `phases/<phase>` from its `phase` field; a slice's parent is its
   `phase`. Rows without a string `id` are skipped.
2. Each phase node starts with an empty list, so a phase with no tasks is
   listed.
3. For each task row the projection walks up from its parent to the first
   `phase` node. The walk stops after as many steps as the hierarchy has
   nodes, so a parent cycle or a parent that is not a node leaves the task
   under no phase.
4. The task's state is `out_of_scope` when the row sets `out_of_scope`, else
   its `state`, else `done` when `done` is true, else `open`.

`phases.phase_changes` then ignores `out_of_scope` tasks, as before: a phase
with every remaining task `done` and an unticked checkbox gets a tick, and a
ticked phase holding a task that is not `done` gets a reopen. Because nothing
is read, the old skip line `phase pass skipped, the hierarchy read failed:
<error>` no longer appears.

### Counting the reads

`tests/swarm/tick_scenarios/test_characterisation.py` replays the recorded
`running` scenario through `run_tick` with a counted view:

- `test_a_running_pass_reads_the_ledger_once_plus_once_per_write` asserts no
  `state` call, no `hierarchy` call, at least one delta read and at most one
  plus the number of ledger writes, and no delta read that follows another
  with no write between them.
- `test_a_planted_second_read_without_a_write_is_counted` marks the view stale
  with no write and asserts the counter reports that read, so the first test
  cannot pass by counting nothing.

The writes counted are the fake ledger's `update_task`, `notify`,
`mark_swarm`, `rank_task`, `comment`, `join` and `notice`.

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
  entry in the same change, which is how the list only shrinks;
- the base has no allowlist file, with the error
  `tests/WHOLE_LEDGER_ALLOWLIST.json is missing from the base`.

A graded file that does not parse fails the gate with only
`::error::cannot parse <file>: <parser error>` and no summary line. Otherwise
the output is one summary line and one `::error::` line per error:

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
| A step does not see a write it made earlier in the pass | The write did not go through `LedgerClient` with operations, so `view.wrote()` never marked the view stale. Route the write through the ledger client, or call `view.wrote()` after it |
| The phase pass misses a task, or ticks a phase early | The projection reads the pass document: check the task's `slice` or `phase` field leads to a phase row. A task whose parent chain loops or names a missing node sits under no phase |
| A step's `pass_state` result lacks a top level part | The part is not in `view.PARTS`. Add it there and to the export route's `parts` enum |
| Lint fails with `reads the whole ledger state N times` | The change added a whole ledger read in the swarm or tick package. Read through `pass_state` instead |
| Lint fails with `differs from the head's measurement` | The allowlist was not rerecorded after removing or moving a read. Run `python -m scripts.no_whole_ledger --write` and commit the file |

## Tests

- `tests/tick/test_view.py`: first full read, one delta call patching the
  stored view, an exceeded change window, a patch that does not fit, an
  unchanged generation, `start` reading through the ledger client, the view
  holding every top level part the pass reads, `pass_state` reading each
  document through one delta read, and `document` serving the held view and
  reading again only after a write.
- `tests/swarm/test_phase_projection.py`: each phase's tasks listed through
  slices with tasks outside any phase skipped, a document without tasks
  listing each phase empty, and the walk stopping on a cycle of two slices that
  name each other in their `phase` field.
- `tests/swarm/tick_scenarios/test_characterisation.py`: a running pass reads
  the ledger once plus once per write, and a planted second read is counted.
- `tests/swarm_ledger/test_export_parts.py`: `parts` on full and change
  exports, events only with `_meta`, a changed root, one cached export per part
  set, request validation, and the client's `changes` call.
- `tests/swarm/test_tick_view_start.py`: the pass starts the view once before
  its first ledger read, and never for a stand-in ledger; a pass holding a
  view makes no whole ledger read; a stopped pass reads its ledger through the
  view.
- `tests/test_no_whole_ledger.py`: what counts as a whole ledger read, the
  shrink only grade against the base, a stale allowlist, and `--write`.
