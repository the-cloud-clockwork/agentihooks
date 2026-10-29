# Ledger API v1 contract

All routes retain the host check. Ledger resources authenticate the stored operator credential or the credential bound to the named worker. Worker author and role checks and domain gates still apply. Origins, when supplied, must be allowed. Layout and bin administration require the existing operator origin. Uploads retain joined uploader and credential binding checks.

| Current operation | Versioned resource | Schema and reply |
| --- | --- | --- |
| Home summaries and bin summaries | GET `/api/v1/ledgers`, GET `/api/v1/bin` | Unique limit and cursor query; paginated summaries |
| Ledger metadata and summary item | GET `/api/v1/ledgers/{slug}`, GET `/metadata` | Bounded metadata; histories, members and seeds excluded |
| Tasks, phases, questions, followups, notes | GET `/{collection}`, GET `/{collection}/{item}` | Paginated collection; item excludes thread contents |
| Comments and question answers | GET `/{collection}/{item}/{thread}`, GET `/threads` | Paginated thread or flat entries for composite consumers |
| Chat, priorities, notifications, artifacts and artifact trash | GET `/{collection}`, GET `/{collection}/{item}` | Paginated collection or bounded item |
| Members, events and sources | GET `/{collection}` | Paginated resource; members also have item reads |
| Swarm controls and agents, history, findings, transfers, handoffs, gates | GET `/swarm`, GET `/swarm/{collection}` | Bounded control metadata and paginated collections |
| Layout | GET/PUT `/api/v1/layout` | Schema checked row sizes; bounded saved resource |
| Bin delete and restore | POST `/api/v1/bin/actions` | Schema checked action and slug; acknowledgment |
| Start, pause, stop, stop now, close, reopen, set, terminate, verdict, restore decision, lift, quota refresh, Doctor start and stop | POST `/swarm/actions` | Central control schema plus existing domain validation; acknowledgment |
| Media and artifact upload | POST `/uploads/media`, POST `/uploads/artifacts` | Schema checked length, content type and artifact name; existing binary validation; file descriptor |
| Media and artifact files | GET `/files/media/{id}`, GET `/files/artifacts/{id}` | The stored file under its content hash name with its media type and sandbox policy; no credential, so an image tag can load it |
| Ledger page saves | PUT `/saves` | The page's queued operations and checkbox changes, with the same author checks as the old page save and no revision guards; acknowledgment, the page takes state from the event stream |
| Ledger page swarm controls and model list | PUT `/swarm/controls`, GET `/swarm/models` | The page's control request, lifecycle actions included, answered with fresh swarm status; the model catalog of both harnesses |
| Live changes for pages and watchers | GET `/events` with `Accept: text/event-stream` | Server sent events: snapshot, then ledger and swarm patches and heartbeats; the ledger carries `marks` (plan, phase, slice and task addresses that are frozen, or held by a focus, by the server freeze and focus rules: a focus on a Doctor's watched swarm holds its tasks too, and urgent tasks and Doctor fix phase tasks are never held) and `_meta.limits` (capacity bounds, effort ladder, sync cooldown, artifact bin days); `Last-Event-ID` replays retained events; an unknown or expired cursor answers 410 `cursor_expired` |
| Plan hierarchy | GET `/hierarchy`, GET `/hierarchy/{read}`, GET `/hierarchy/{read}/{collection}/{item}` | `children`, `ancestors`, `subtree` (default) or `dependents` of a plan, phase, slice or task, or of the ledger root, read through recursive joins on the hierarchy tables; rows paged by `limit` and `cursor` like the other collections (a tree changed between pages answers 409 `revision_conflict` and readers start again from the first page) of node, revision, kind, parent, depth (distance from the asked node; the ledger's top nodes at 0) and state (a phase's lifecycle) in display order; an unknown read or node answers 404 `resource_missing` |
| Task work folder lines | GET `/tasks/{task}/workspace` | Latest progress and proof lines of one task, read when its proof fold opens; an unsafe task identifier answers 404 `resource_missing` |
| Explicit large export and comment audit | POST `/export`, POST `/swarm/export` | Empty object request; complete ledger without seeds or receipt index, or complete swarm status |

Relative routes are under `/api/v1/ledgers/{slug}`. Old routes remain available.

All domain mutations use POST or PUT `/operations`. The envelope has a transport operation identifier separate from domain entry identifiers, at most one hundred operations and a guards map from affected resource paths to their expected content revisions. Checkbox changes have a distinct envelope identifier, Boolean values and the same resource guards. All schemas and domain requests are checked before mutation. Missing guards return `revision_required`, stale guards return `revision_conflict` with the stale resource path in `details.path`, reused identifiers with different content return `operation_conflict`, invalid schemas return `schema_invalid`, and unauthorized callers return `forbidden`. Errors use an `error` object with stable `code`, `message` and optional `details` fields.

| Domain operation family | Operations covered | Guard resource |
| --- | --- | --- |
| Threads | add, edit, delete, clear | Named thread |
| Reconciliation | sync, stats_sync | Metadata |
| Membership | join, leave, agent_rename; ack needs no guard because it only raises the caller's handled revision | Members |
| Agent state | claim, set, add_item, retext, gate_bypass, gate_lift | Named item, list or metadata |
| Tasks | task_add, task_update, task_rank, task_group, task_ungroup | Named task or tasks collection; task_add needs no guard because the locked apply refuses an id already in use, so adds of different ids never conflict |
| Phases | phase_add, phase_update, phase_review, phase_append | Phases collection or named phase |
| Decisions | priority, priority_clear, notification_clear, relay, answer, verdict | Priority or notification collection, or named item |
| Ledger lifecycle | title_set, overview_set, summary_set, close, reopen, size_set, source_add | Metadata or sources |
| Artifacts | artifact_add, artifact_delete, artifact_restore, artifact_purge | Artifacts |

Operation schema names are checked against every domain dispatch name. Existing semantic validators are reused after the central strict field and type schemas. Receipts retain the latest one thousand transport identifiers and operation outcomes in repository metadata, including across server restarts. A replay within that retained window ignores its old expected revision and does not apply again. Outside the window the revision guard still applies.

Collections return at most one hundred entries. Routine replies fit within 256 KiB including their envelope. Cursors carry the collection content revision and fail on a changed collection. Thread counts replace embedded histories in item reads; composite clients restore threads from bounded pages. Oversized individual resources require the explicit export. Content revisions use SHA256 of sorted, compact UTF8 JSON and do not change when unrelated items change. Metadata revision excludes global revision and update time bookkeeping.

The command client preserves operation identifiers and expected revisions across transport retry. Writes return acknowledgments and requested task updates. Tick task, event, chat and closed reads request their resources. Clients needing a composite state assemble bounded resource pages and check the ledger revision again before using the result. Full comment audit requests the named export.
