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
`cache_scope_mismatch_total` (`hooks.context.project_cache`, per kind, in `cache-scope-mismatches.json`) and
logged by kind only.

| Key | Kind and parts | Namespace |
|---|---|---|
| Brain feed snapshot | `feed` | installation, brain |
| Project memory cache | `project-memory`, remote or repo | installation, brain, project |
| Deferred project context | `pending`, session ID | installation, brain |
| Last published feed hash | `publish-hash` (single file, stamped) | installation, brain |
| Brain marker idempotency key | uuid5 of format, namespace, session ID, type, task and content | installation, brain, project |

## Marker keys and live sessions

Every Stop re-sends every marker in the transcript and relies on the idempotency key to deduplicate. A marker
whose transcript time is before the installation's creation time, or that has no valid time, keeps the legacy key
(session, type and content), so a live session never reposts its old markers under a new key. Markers at or after
the creation time use the namespaced key, which also carries the task, so the same text under two tasks of one
session is two markers. An outbox file records the key of its first post and a replay sends that key unchanged,
whichever brain is configured at replay; an outbox file without one replays with the legacy key.

## Migration and rollback

- Legacy cache files (`feed.json`, `<24 hex>.json`, `pending-<24 hex>.json`) are never read. Each feed store
  removes at most 32 of them (`sweep_legacy`); they are rebuildable caches.
- The legacy publish hash file is read as empty, so the next refresh rewrites it stamped.
- Rollback: revert the change, then `python -m scripts.swarm_v2.keyspace drop <agentihooks home>/brain/project-memory`
  removes only `k2-` cache files. It never touches the session index, scope logs, the outbox, the installation
  record or legacy files; the preceding code rebuilds its own caches.

## Not covered

- Hook session state in Redis (`redis_key`: file read cache, retry breaker, branch and PR signals, transcript
  positions, token counters) stays keyed by `REDIS_KEY_PREFIX` and the native session ID. Installations that share
  one Redis must set distinct prefixes until a later package namespaces those keys.
- Kernel outbox drains (`agentibrain sync`, brain-ops `outbox_drain`) still compute the legacy key and ignore the
  recorded one; within the brain's one hour idempotency window such a drain can duplicate a marker first posted
  under a namespaced key.
- No session transcript archive exists yet; its identities belong to the session memory packages and must be
  built on this namespace.
