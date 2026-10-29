"""Recall MCP tools — read the recall archive of ledgers and swarms.

Every tool is read only. scope "" means the caller's own swarm (all sources
outside a swarm), a ledger or swarm slug narrows it, and "all" crosses swarms.
"""

import json

from hooks.common import log


def _respond(name: str, call) -> str:
    try:
        from scripts.recall import tools

        return json.dumps(call(tools))
    except Exception as e:
        log(f"MCP {name} failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


def register(mcp):
    @mcp.tool()
    def recall_search(
        query: str,
        scope: str = "",
        kinds: str = "",
        author: str = "",
        seat: str = "",
        period: str = "",
        limit: int = 10,
    ) -> str:
        """Search tasks, phases, comments, chat, handoffs and learned notes of earlier
        ledgers and swarms. Use when the operator says remember, yesterday, or names
        earlier work. A task id, ref or pull request number in the query comes first.

        Args:
            query: words, a task id such as rc6, a ref such as tasks/rc6, or a PR number such as #6774.
            scope: "" for your own swarm, a ledger or swarm slug, or "all" for every swarm.
            kinds: comma separated record kinds, e.g. "task,comment,handoff".
            author: only records by this author.
            seat: only records under seats/<seat>/.
            period: "SINCE", "SINCE..UNTIL" or "..UNTIL"; each an ISO time, epoch milliseconds
                or a span back from now such as 1d or 3h.
            limit: most hits returned.
        """
        return _respond(
            "recall_search",
            lambda tools: tools.search(query, tools.filters(scope, kinds, author, seat, period), limit),
        )

    @mcp.tool()
    def recall_get(ref: str, scope: str = "") -> str:
        """Return one recall item in full: its whole text, parent chain and comments.

        Args:
            ref: an item key from recall_search ("<ledger>/tasks/rc6") or a ref inside the scope ("tasks/rc6").
            scope: as in recall_search.
        """
        return _respond("recall_get", lambda tools: tools.get(ref, scope))

    @mcp.tool()
    def recall_timeline(scope: str = "", kinds: str = "", period: str = "", limit: int = 200) -> str:
        """List recall items in time order, oldest first.

        Args:
            scope: as in recall_search.
            kinds: as in recall_search.
            period: as in recall_search.
            limit: most entries returned.
        """
        return _respond(
            "recall_timeline", lambda tools: tools.timeline(tools.filters(scope, kinds, period=period), limit)
        )

    @mcp.tool()
    def recall_sources() -> str:
        """List the indexed ledgers and swarms with record and item counts, last
        indexed time (epoch milliseconds) and whether the ledger sits in the bin."""
        return _respond("recall_sources", lambda tools: tools.sources())
