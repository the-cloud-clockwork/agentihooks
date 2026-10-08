# Ledger reader migration inventory

Closed. SQLite (`ledgers.sqlite3` in the ledger folder, `SQLiteLedgerRepository`) is the only durable ledger
record. No normal read or write opens a ledger JSON document or HTML page; the only code that reads those files is
the one time legacy import in the repository's `legacy` module.

Evidence: reproduce with
`rg -n 'read_ledgers?\(|read_document\(|read_slugs\(|read_ids\(|read_registry\(|repository\.[a-z_]+\(' hooks scripts --glob '*.py'` for the call
sites below, and `rg -n 'core\.paths|load_state|parse_seed|read_token|\{slug\}\.(json|html)' hooks scripts --glob
'*.py'`, which finds no ledger document reader outside the repository's `legacy` module (the remaining hits are
session, telemetry and install state files).

| Consumer symbol | Resource | Read or write through |
| --- | --- | --- |
| ledger_core.sync | whole ledger | facade over repository.apply_ops |
| ledger_server.page_for | title, token | repository.read(slug, "title"), repository.token |
| ledger_server.Handler.exists, refused, reply_state, receive, post_media, post_artifact | document, members, token | repository.exists, token, read(slug, "_meta.members"), get_document, apply_ops |
| ledger_server.stream_resources, all_summaries, serve | ledger view, summaries | repository.get_document, list_summaries |
| ledger_server.watch_ledgers | bin sweep only | bin_storage; the page seed scan is removed |
| api.routes, api.mutations | v1 resources and operations | server.repository.get_document, token, apply_ops |
| ledger.credentials, call | token, existence | repository.token, exists; reads go to the v1 export route |
| ledger.cmd_show | whole ledger, read only | v1 export through ResourceClient.snapshot |
| new_ledger.create, main | new ledger | repository.create, exists, apply_ops, list_summaries |
| ledger_bin.delete, restore, entries, restored, auto_bin, purge_expired, bin_closed | bin and restore marks | repository.delete, restore; bin_storage over the registry table |
| ledger_hook.main | gate reads for a bound session | read_ledger(LEDGER_DIR, slug, GATE_READS), read only connection |
| ledger_hook.serve_ledgers | whether any ledger exists | database file or a legacy JSON waiting for import |
| watch_ledger.main | existence, event stream | repository.exists, then the v1 event stream |
| swarm_refocus._read_ledger | title, overview, phases, task or priorities | read_ledger, read only |
| plan_read_guard._plans | artifacts, phases and task of this ledger, artifacts of every ledger | read_ledger, read_ledgers, read only |
| recall.reindex.reindex, binned, all_slugs; recall.cli.main | whole ledger with events, bin registry, stored slugs | read_document, read_registry, read_slugs, read only; explicit backfill command |
| ledger_request._ledger | members, task | read_ledger, read only |
| project_cache._swarm_overview | overview | read_ledger, read only |
| ledger_decision._bound | bound ledger still live | read_registry(bin), read_ledger(title) |
| correlation task lookup | task row | read_ledger(root, slug, "tasks/<id>") |
| gates.build.task_territory, task_ids | task territory, task ids | read_ledger, read_ids |
| swarm.prompt.summary_lines | overview | read_ledger, read only |
| swarm.prompt.ledger_read | how an agent reads the ledger | names `agentihooks ledger --slug S show` |
| swarm.profile_choice._ledger | overview, phases | read_ledger, read only |
| swarm.snapshot.take, restore | whole ledger in a snapshot | repository.export_document; repository.import_document on restore |
| swarm.resume, master_launch | ledger read instruction | snapshot.ledger_source |
| storage_migration export, load, cutover | interchange | repository.export_document, import_document, list_summaries |

Retired: the file repository, the shadow repository, per operation JSON and HTML rewrites, the HTML seed scan and
merge, `new_ledger --upgrade`, and the whole document `GET /api/<slug>` (answers 410). The page `PUT /api/<slug>`
answers a bounded acknowledgment; the page takes state from the event stream.

Adjacent persistence, not ledger documents: ledger session bindings under `.sessions`, operator mode bindings,
swarm snapshot files (which embed an exported ledger), Redis swarm, inbox and health state, templates, palette,
media, artifacts, process markers, task work folders and config JSON.
