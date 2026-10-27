"""Consumer inventory for retiring the legacy transcript service: each caller gets an agentihooks target or removal."""

import copy
import json

from hooks.memory.api.ingest import Rejected
from hooks.memory.api.records import ALIASES

ALIAS, REPLACE, REMOVE = "alias", "replace", "remove"
UNMAPPED = "migration_unmapped_consumers"
KINDS = frozenset({"tool", "cli", "dashboard", "ingestion", "brain_extraction", "deployment"})
ENTRY_FIELDS = frozenset({"consumer", "kind", "uses"})
SOURCE_LICENSE = "MIT"
EXTRACTABLE = {
    "turn_chunking": "hooks.memory.chunking",
    "vector_storage": "hooks.memory.embeddings",
    "semantic_search": "hooks.memory.search",
}
REPLACEMENTS = {
    "get_session_actions": "session_records",
    "agent_search": "session_search",
    "search_history": "session_search",
    "collect_now": "hooks.memory.collector",
    "list_plans": "agentihooks plan read",
    "get_plan": "agentihooks plan read",
    "/sessions": "session_list",
    "mcp_tool_grant": "agentihooks session tools",
    "postgres_history": "hooks.memory.importer",
}
RETIRED = {
    "dispatcher": (
        "dispatch_task",
        "get_dispatch_job",
        "list_dispatch_jobs",
        "plan_task",
        "get_dispatch_plan",
        "list_dispatch_plans",
        "execute_plan",
        "run_agent",
        "dispatch_to_agent",
        "/dispatch/jobs",
        "dispatch_url",
    ),
    "agent_registry": (
        "register_agent",
        "deregister_agent",
        "heartbeat_agent",
        "list_agents",
        "get_agent",
        "find_agents",
        "discover_local_agents",
        "/agents",
    ),
    "standalone_daemon": ("/health", "service_deployment", "daemon_installer"),
    "oauth_server": ("/authorize", "/token", "/register"),
    "handoff_service": ("list_handoff_projects", "handoff"),
    "memory_files": ("list_memory_files", "get_memory_file"),
    "summary_synthesis": ("generate_summary",),
    "session_restore": ("restore_session",),
}
RETIRED_BY_USE = {use: feature for feature, uses in RETIRED.items() for use in uses}
NOT_TOOLS = frozenset({"service_deployment", "daemon_installer", "dispatch_url"})
LEGACY_TOOLS = (
    *ALIASES,
    *(use for use in REPLACEMENTS if not use.startswith("/") and use not in ("mcp_tool_grant", "postgres_history")),
    *(use for use in RETIRED_BY_USE if not use.startswith("/") and use not in NOT_TOOLS),
)
CONSUMERS = (
    *({"consumer": f"gateway legacy MCP tool {name}", "kind": "tool", "uses": name} for name in LEGACY_TOOLS),
    {"consumer": "antoncore landing sessions indexed figure", "kind": "dashboard", "uses": "/sessions"},
    {"consumer": "antoncore landing legacy health figure", "kind": "dashboard", "uses": "/health"},
    {"consumer": "antoncore landing dispatch job figures", "kind": "dashboard", "uses": "/dispatch/jobs"},
    {"consumer": "antoncore landing workforce heartbeat enrichment", "kind": "dashboard", "uses": "/agents"},
    {"consumer": "antoncore blackbox exporter health probe", "kind": "deployment", "uses": "/health"},
    {"consumer": "antoncore gateway OAuth routes", "kind": "deployment", "uses": "/authorize"},
    {
        "consumer": "antoncore ArgoCD application, chart, ingress and image updater",
        "kind": "deployment",
        "uses": "service_deployment",
    },
    {"consumer": "antoncore agent chart environment service URLs", "kind": "deployment", "uses": "dispatch_url"},
    {"consumer": "antoncore LiteLLM unit and gateway MCP grants", "kind": "deployment", "uses": "mcp_tool_grant"},
    {"consumer": "antoncore backup of the legacy session databases", "kind": "ingestion", "uses": "postgres_history"},
    {
        "consumer": "agentibrain reasoner feedback dispatch hint",
        "kind": "brain_extraction",
        "uses": "dispatch_to_agent",
    },
    {"consumer": "agentibrain keeper MCP tool permission", "kind": "brain_extraction", "uses": "mcp_tool_grant"},
    {"consumer": "agentibrain keeper dispatch service URL", "kind": "brain_extraction", "uses": "dispatch_url"},
)
BAD_ENTRY = "a consumer entry needs exactly a consumer name, a known kind and the legacy capability it uses"
STALE = "the inventory moved past this revision; read it again and resubmit the consumer"
RETIRED_COPY = "this legacy feature is retired; it is not copied even where it shares a module with search code"
NOT_EXTRACTABLE = "only turn chunking, vector storage and semantic search may be extracted"


def _valid(entry: object) -> bool:
    if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
        return False
    if not all(isinstance(entry[key], str) and entry[key] for key in ENTRY_FIELDS):
        return False
    return entry["kind"] in KINDS


def classify(entry: object) -> dict:
    if not _valid(entry):
        raise Rejected("bad_consumer", BAD_ENTRY)
    use = entry["uses"]
    if use in ALIASES:
        return {**entry, "decision": ALIAS, "target": ALIASES[use]}
    if use in REPLACEMENTS:
        return {**entry, "decision": REPLACE, "target": REPLACEMENTS[use]}
    if use in RETIRED_BY_USE:
        return {**entry, "decision": REMOVE, "retired": RETIRED_BY_USE[use]}
    return {**entry, "decision": None}


def extract(component: str, module: str) -> dict:
    if component in RETIRED or component in RETIRED_BY_USE:
        raise Rejected("retired_feature", RETIRED_COPY)
    if component not in EXTRACTABLE:
        raise Rejected("not_extractable", NOT_EXTRACTABLE)
    return {
        "component": component,
        "source_module": module,
        "target": EXTRACTABLE[component],
        "source_license": SOURCE_LICENSE,
        "copied": False,
    }


class Inventory:
    def __init__(self, state: dict | None = None) -> None:
        self.state = copy.deepcopy(state) if state else {"revision": 0, "items": {}}

    def add(self, entry: object, revision: int) -> dict:
        item = classify(entry)
        held = self.state["items"].get(item["consumer"])
        if held == item:
            return {"item": held, "replayed": True}
        if revision <= self.state["revision"]:
            raise Rejected("stale_revision", STALE)
        self.state["items"][item["consumer"]] = item
        self.state["revision"] = revision
        return {"item": item, "replayed": False}

    def report(self) -> dict:
        items = [self.state["items"][name] for name in sorted(self.state["items"])]
        unmapped = [item["consumer"] for item in items if item["decision"] is None]
        return {"revision": self.state["revision"], "items": items, UNMAPPED: len(unmapped), "unmapped": unmapped}

    def dumps(self) -> str:
        return json.dumps(self.state, sort_keys=True)

    @classmethod
    def loads(cls, text: str) -> "Inventory":
        return cls(json.loads(text))
