# Search consumer migration (SV2-MIG-01)

The legacy transcript service (agentibridge) is retired. This page lists every known caller and what each one
moves to. The inventory itself is code: `hooks.memory.compatibility.CONSUMERS`, classified by
`hooks.memory.compatibility.classify`. A consumer whose capability has no target or removal decision is counted in
`migration_unmapped_consumers`; the known inventory holds zero.

## Reusable search logic

Only three components may be taken from the legacy service, and each already has an agentihooks implementation.
`hooks.memory.compatibility.extract` names the target and records the source license (MIT). No legacy source was
copied, so no attribution notice is carried in code.

| Component | agentihooks implementation |
|---|---|
| Turn chunking | `hooks.memory.chunking` |
| Vector storage | `hooks.memory.embeddings` and `hooks.memory.index_worker` |
| Semantic search | `hooks.memory.search`, served by the `session_search` tool |

The dispatcher, agent registry, standalone daemon and its installer, OAuth server, handoff service, memory file
tools, summary synthesis and session restore are retired. `extract` refuses them even where they share a module
with search code.

## Decisions

| Decision | Meaning |
|---|---|
| alias | A legacy tool name answered by the agentihooks session tools, read from `hooks.memory.api.records.ALIASES`. Aliases stay off unless `AGENTIHOOKS_SESSION_ALIASES=1` and expire on 2027-01-31. |
| replace | The caller moves to a named agentihooks interface. |
| remove | The caller depends on a retired feature and is deleted with it. |

### Legacy MCP tools

| Tool | Decision | Target or retired feature |
|---|---|---|
| list_sessions | alias | session_list |
| get_session | alias | session_get |
| get_session_segment | alias | session_records |
| search_sessions, search_semantic | alias | session_search |
| get_session_actions | replace | session_records |
| agent_search, search_history | replace | session_search |
| collect_now | replace | the session collector, `hooks.memory.collector` |
| list_plans, get_plan | replace | `agentihooks plan read` |
| dispatch_task, get_dispatch_job, list_dispatch_jobs, plan_task, get_dispatch_plan, list_dispatch_plans, execute_plan, run_agent, dispatch_to_agent | remove | dispatcher |
| register_agent, deregister_agent, heartbeat_agent, list_agents, get_agent, find_agents, discover_local_agents | remove | agent registry |
| list_handoff_projects, handoff | remove | handoff service; swarm handoffs replace it |
| list_memory_files, get_memory_file | remove | memory files |
| generate_summary | remove | summary synthesis |
| restore_session | remove | session restore |

### Other consumers

| Consumer | Kind | Decision | Target or retired feature |
|---|---|---|---|
| antoncore landing sessions indexed figure | dashboard | replace | session_list over the session catalog |
| antoncore landing legacy health figure | dashboard | remove | standalone daemon |
| antoncore landing dispatch job figures | dashboard | remove | dispatcher |
| antoncore landing workforce heartbeat enrichment | dashboard | remove | agent registry |
| antoncore blackbox exporter health probe | deployment | remove | standalone daemon |
| antoncore gateway OAuth routes | deployment | remove | OAuth server |
| antoncore ArgoCD application, chart, ingress and image updater | deployment | remove | standalone daemon |
| antoncore agent chart environment service URLs | deployment | remove | dispatcher |
| antoncore LiteLLM unit and gateway MCP grants | deployment | replace | agentihooks session tools |
| antoncore backup of the legacy session databases | ingestion | replace | `hooks.memory.importer` reads the historical data; the databases stay read only until imported |
| agentibrain reasoner feedback dispatch hint | brain extraction | remove | dispatcher |
| agentibrain keeper MCP tool permission | brain extraction | replace | agentihooks session tools |
| agentibrain keeper dispatch service URL | brain extraction | remove | dispatcher |

The antoncore and agentibrain changes are their own pull requests in those repositories; antoncore changes go
through GitOps. This package changes neither.

## Late consumers

A consumer found later is added with `Inventory.add` at a newer revision. Its capability is classified the same way,
so a late dispatcher caller is removed, never given a dispatcher back. Replaying the same entry changes nothing; a
different entry at a stale revision is refused.

## Rollback

Leave `AGENTIHOOKS_SESSION_ALIASES` unset until a mapping has been validated against its callers. The legacy session
databases are authoritative history, not a cache: keep them until `hooks.memory.importer` has read them.
