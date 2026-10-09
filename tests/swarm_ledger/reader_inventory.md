# Ledger reader migration inventory

Evidence: source search at base commit 4d6e11b6, followed by symbol reads and the extraction diff. Reproduce with `rg -n 'development-ledger|LEDGER_DIR|core\.paths|load_state|parse_seed' hooks scripts` and `rg -n 'read_text|read_bytes|json\.load' hooks/context scripts/swarm scripts/swarm_ledger scripts/gates scripts/doctor`.

| Consumer symbol | Resource | Migration status |
| --- | --- | --- |
| ledger_core.load_state, ledger_core.sync | JSON document, HTML seed | Compatibility facades now delegate to file repository |
| ledger_server.all_summaries, page_for, swarm_status, Handler.exists, Handler.refused, Handler.reply_state, Handler.receive, watch_seeds | JSON document, HTML page, token, page metadata | Repository owns document reads, operations and summaries; file adapter serves legacy page and token access |
| ledger.request, ledger.upload | HTML token | File adapter read_page |
| new_ledger.create, upgrade_page, main | HTML page and JSON document | Repository create, get_document, apply_ops and page adapter |
| ledger_bin.entries, restored, auto_bin, purge_expired, delete, restore, bin_closed | Bin indexes, JSON documents, HTML enumeration | File implementation owns persistence; pure eligibility and retention decisions remain unchanged |
| ledger_hook.read_json via main | JSON document | Later migration; read-only hook fallback |
| ledger_hook.post_bypass | HTML token | Versioned operations route through ledger.request |
| chat_ledger.main | HTML token | Versioned operations route through ledger.request |
| watch_ledger.read via main | JSON document event stream | Later migration to events_since |
| swarm_refocus._read_json via build | JSON document | Later migration; startup and compaction context |
| ledger_request._ledger | JSON document | Later migration; task and intent lookup |
| project_cache._read via _swarm_overview | JSON document | Later migration; currently uses default ledger folder |
| swarm.prompt.summary_lines | JSON overview | Later migration; master launch context |
| swarm.snapshot.take | JSON document embedded in snapshot | Later migration; restoration format must stay compatible |
| gates.build.task_territory | JSON document | Later migration; task ownership and territory check |

Adjacent persistence: operator_mode._binding and ledger_decision._bound read ledger session binding JSON; they do not read ledger documents. Swarm snapshot newest, info and restore read snapshot JSON, including a saved ledger document. These need separate session and snapshot boundaries. Redis JSON readers in swarm store, inbox and health are not ledger file readers.

Swarm LedgerClient, resume and Doctor ledger access use the ledger command or its request transport rather than reading ledger documents directly. Pure domain modules are unchanged. Templates, palette, media, artifacts, process markers, task work folders and config JSON have separate consumers and remain outside document storage.
