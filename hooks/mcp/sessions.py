"""Session memory MCP tools: list, search, get and record range expansion over the session catalog.

Registered only when AGENTIHOOKS_SESSION_CATALOG names the catalog; unset, the tools are hidden and the catalog
and archive stay as they are. The grant comes from the operator's AGENTIHOOKS_SESSION_CORPUS and
AGENTIHOOKS_SESSION_PROJECTS, never from a tool argument. Search results are evidence, not resume commands.
"""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.common import log

if TYPE_CHECKING:
    from hooks.memory.api.ingest import Grant

CATALOG_ENV = "AGENTIHOOKS_SESSION_CATALOG"
OBJECTS_ENV = "AGENTIHOOKS_SESSION_OBJECTS"
CORPUS_ENV = "AGENTIHOOKS_SESSION_CORPUS"
PROJECTS_ENV = "AGENTIHOOKS_SESSION_PROJECTS"
ALIASES_ENV = "AGENTIHOOKS_SESSION_ALIASES"
TOOL_FAILED = "the session tool failed before answering; the log names the cause"

_tools = None


def _session_tools():
    global _tools
    if _tools is None:
        from hooks.memory import archive
        from hooks.memory.api.records import SessionTools
        from hooks.memory.catalog import Catalog

        catalog = Path(os.environ[CATALOG_ENV])
        objects = os.getenv(OBJECTS_ENV) or str(catalog.parent / "objects")
        _tools = SessionTools(Catalog(catalog), archive.ObjectStore(objects))
    return _tools


def grant() -> "Grant":
    from hooks.memory.api.ingest import Grant

    projects = frozenset(item.strip() for item in os.getenv(PROJECTS_ENV, "").split(",") if item.strip())
    return Grant(os.getenv("AGENTIHOOKS_AGENT_NAME", ""), os.getenv(CORPUS_ENV, ""), projects)


def respond(name: str, build: Callable[[], dict]) -> str:
    try:
        fields = build()
        from hooks.memory.api import records
        from hooks.memory.api.ingest import SCHEMA_VERSION

        caller = grant()
        request = {"schema_version": SCHEMA_VERSION, "access_domain": caller.corpus, **fields}
        return json.dumps(records.call(_session_tools(), name, caller, request, time.time()))
    except Exception as e:
        log(f"MCP {name} failed", {"error": str(e)})
        return json.dumps({"success": False, "error": TOOL_FAILED})


def _projects(projects: str) -> dict:
    listed = [item.strip() for item in projects.split(",") if item.strip()]
    return {"projects": listed} if listed else {}


def _cursor(cursor: str) -> dict:
    return {"cursor": cursor} if cursor else {}


def _alias(mcp, name: str, target: str) -> None:
    def alias(request: str = "{}") -> str:
        return respond(name, lambda: json.loads(request))

    alias.__doc__ = f"Compatibility alias for {target}; takes the {target} request as a JSON object and expires."
    mcp.tool(name=name)(alias)


def register(mcp):
    if not os.getenv(CATALOG_ENV):
        return

    @mcp.tool()
    def session_list(projects: str = "", limit: int = 20, cursor: str = "") -> str:
        """List retained sessions inside your grant with harness, projects, tasks, source timestamps and sources.

        Args:
            projects: comma separated projects narrowing your grant; empty for every granted project.
            limit: most sessions per page, 1 to 50.
            cursor: next_cursor from an earlier page of the same listing.
        """
        return respond("session_list", lambda: {**_projects(projects), "limit": limit, **_cursor(cursor)})

    @mcp.tool()
    def session_search(query: str, projects: str = "", limit: int = 10, context: int = 1) -> str:
        """Search session evidence inside your grant. Each hit carries a citation for session_records.

        Args:
            query: words, identifiers or "quoted phrases".
            projects: comma separated projects narrowing your grant.
            limit: most hits, 1 to 50.
            context: neighbouring chunks shown on each side, 0 to 3.
        """
        return respond(
            "session_search", lambda: {"query": query, **_projects(projects), "limit": limit, "context": context}
        )

    @mcp.tool()
    def session_get(session_uid: str) -> str:
        """Return one session's harness, projects, tasks, source timestamps and sources.

        Args:
            session_uid: a session_uid from session_list or a search hit.
        """
        return respond("session_get", lambda: {"session_uid": session_uid})

    @mcp.tool()
    def session_records(citation: str, before: int = 0, after: int = 0, limit: int = 50, cursor: str = "") -> str:
        """Expand a search hit's citation to the exact records and archived source text it was built from.

        Args:
            citation: the citation object of a search hit, as JSON.
            before: extra records before the cited range, 0 to 20.
            after: extra records after the cited range, 0 to 20.
            limit: most records per page, 1 to 200.
            cursor: next_cursor from an earlier page of the same expansion.
        """

        def fields() -> dict:
            return {
                "citation": json.loads(citation),
                "before": before,
                "after": after,
                "limit": limit,
                **_cursor(cursor),
            }

        return respond("session_records", fields)

    if os.getenv(ALIASES_ENV) == "1":
        from hooks.memory.api.records import ALIASES

        for name, target in ALIASES.items():
            _alias(mcp, name, target)
