# Cache and deduplication keyspace, format 2

SV2-IDN-05 keeps two installations, or two brains on one installation, from sharing a cache entry or a brain
deduplication key when they share a native session ID, a path and every display label. The interface is
`scripts.swarm_v2.keyspace`.

## Identities

- **Installation**: `installation(home)` reads `installation.json` under the agentihooks home. The first call
  creates it with a random `inst-<32 hex>` ID and its creation time, through a temporary file linked into place,
  so concurrent first calls agree on one record. A record that fails the grammar raises `ValueError`; it is never
  replaced. Two hosts with the same home path get different IDs.
- **Brain**: `brain_identity(url=..., path=...)` hashes a secret-free normalized identity. A URL keeps only the
  lowercased scheme and host, a non-default port and the path without its trailing slash; credentials, query and
  fragment are dropped before hashing (`url-<32 hex>`). A file source hashes its resolved path (`file-<32 hex>`).
  No source is `none`. `hooks.context.brain_adapter.brain_id()` applies the adapter's source precedence and
  returns `invalid` for a URL it cannot normalize.
- **Namespace**: installation, brain, canonical project (SV2-IDN-01 `project_id`), policy version and generation.
  Live hooks leave the generation empty until a runtime package registers one.

## Keys

`key(namespace, kind, *parts)` is `k2-<kind>-<32 hex>`, the SHA-256 of the canonical JSON of the namespace, the
kind and the parts. Labels and URLs never appear in a key. Each cached document is written with `stamp`, which
records the full namespace and kind; a reader accepts it only through `admits`, so a document copied or written
under another namespace never satisfies a request. A document that carries another namespace is counted in
`cache_scope_mismatch_total` (`hooks.context.project_cache`, per kind, in `cache-scope-mismatches.json`),
logged by kind only and reported by `brain_status`. It counts refused reads: a foreign document left in place
counts again on every read until a refresh rewrites it.

| Key | Kind and parts | Namespace |
|---|---|---|
| Brain feed snapshot | `feed` | installation, brain |
| Project memory cache | `project-memory`, remote or repo | installation, brain, project |
| Deferred project context | `pending`, session ID | installation, brain |
| Last published feed hash | `publish-hash` (single file, stamped) | installation, brain |
| Brain marker idempotency key | uuid5 of format, namespace, session ID, type, task and content | installation, brain, project |
| Hook session state in Redis (`redis_key`: file read cache, retry breaker, branch and PR signals, controls switch, transcript positions, token counters) | `<REDIS_KEY_PREFIX>:<installation>:<type>:<id>` | installation |

## Marker keys and live sessions

Every Stop re-sends every marker in the transcript and relies on the idempotency key to deduplicate. A marker
whose transcript time is before the installation's creation time, or that has no valid time, keeps the legacy key
(session, type and content), so a live session never reposts its old markers under a new key. Markers at or after
the creation time use the namespaced key. Its project and task come from the session's scope log at the
marker's own time (SV2-IDN-03), never from the current folder or the session index, so every later Stop sends
the same key. The key carries the task, so the same text under two tasks of one
session is two markers. An outbox file records the key of its first post and a replay sends that key unchanged,
whichever brain is configured at replay; an outbox file without one replays with the legacy key.

## Migration and rollback

- Legacy cache files (`feed.json`, `<24 hex>.json`, `pending-<24 hex>.json`) are never read. Each feed store
  removes at most 32 of them (`sweep_legacy`); they are rebuildable caches.
- The legacy publish hash file is read as empty, so the next refresh rewrites it stamped.
- Hook session state in Redis moves to installation keys at the upgrade. Old keys are never read and expire
  with their TTL; the controls switch has none and stays behind, unread. A live session starts again from empty
  state once: retry counts, file read cache, branch and PR signals and the controls switch reset to their safe
  defaults, and the transcript logger re-logs that session's transcript from its first line.
- The first feed store after the upgrade removes undelivered legacy deferred contexts, one per session at most.
- The controls switch (`disable controls`) becomes per installation. Before, every installation on one Redis and
  one prefix shared it; now a pod's switch never reaches the operator's workstation or another pod, and a switch
  set before the upgrade lapses until set again.
- Rollback: revert the change, then `python -m scripts.swarm_v2.keyspace drop <agentihooks home>/brain/project-memory`
  removes only the rebuildable `k2-feed-` and `k2-project-memory-` files. It never touches deferred contexts,
  the session index, scope logs, the outbox, the installation record or legacy files; the preceding code rebuilds
  its own caches and reads its own Redis keys again. The preceding code sends every marker under its legacy key, so a
  marker posted under a namespaced key in the brain's last idempotency hour can be written again, subject to the
  brain's own lesson and signal content checks.
- The installation record must live on persistent storage. A worker whose agentihooks home is rebuilt on each
  start is a new installation each time: its Redis hook state starts empty and its markers re-key from the new
  creation time. Readers outside hooks, such as the status checker, see hook Redis state only under the same home.
- Deleting `installation.json` creates a new installation: its later creation time moves markers stamped
  between the two times back to legacy keys, and the brain can receive them again inside its window.

## Not covered

- Durable Redis records keep their keys: the memory store (`<prefix>:memory:*`) and the event relay streams are
  authoritative data or shared contracts, not caches.
- Kernel outbox drains (`agentibrain sync`, brain-ops `outbox_drain`) send the recorded key from the matching
  agentibrain-kernel change; a kernel release before it recomputes the legacy key.
- A session without a scope log (no SV2-IDN-03 transitions) gives its markers an empty project and task in the
  key, so the same text under two tasks of such a session still deduplicates to the first.
- Markers from a transcript record without a time keep the legacy key permanently. A marker read from the Stop
  payload's last message takes the Stop time.
- The archive clause of the output contract is unmet: no session transcript archive exists yet. Its identities
  belong to the session memory packages and must be built on this namespace.
- `Namespace.generation` has no producer until a runtime package registers execution generations.
