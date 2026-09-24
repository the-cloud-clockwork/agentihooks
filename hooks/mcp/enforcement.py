"""Enforcement MCP tools — operator-curated drumbeat reminders.

Mirrors the channel_* tool surface but for the enforcement system:
- enforcement_set: register a message that re-injects every N tool calls
- enforcement_list: list active enforcements
- enforcement_clear: clear by id, by tag, or all
"""

import json
import os

from hooks.common import log


def _scope_cwd(cwd: str) -> str:
    # The server runs in the session's project directory, so that is the default repo.
    return cwd or os.getcwd()


def register(mcp):
    @mcp.tool()
    def enforcement_set(
        message: str, cadence: int, tag: str = "", matcher: str = "", local: bool = False, cwd: str = ""
    ) -> str:
        """Register a drumbeat enforcement that re-injects every N tool calls.

        Global by default: every session on the machine sees it. A rule that
        belongs to one repository must use local=True, which writes
        <git-root>/.agentihooks/enforcements.json and reaches only sessions
        inside that repository. Permanent until cleared. No severity, no TTL — just a recurring reminder. Cheap because
        re-injection only adds context tokens, no external API calls.

        Args:
            message: Reminder text to inject (e.g. "patches forbidden — code only")
            cadence: Re-inject every N tool calls. Required, must be >= 1.
            tag: Optional tag for grouping (lets you clear-by-tag later).
            matcher: Optional tool matcher; the enforcement is then delivered only on
                matching tool calls and its cadence counts those calls only.
                Grammar: any, bash, edit, mcp, mcp__<server>, mcp__<server>__<tool>,
                bash.<cli> (e.g. bash.kubectl); join alternatives with "+".
            local: Store it in the current repository only (see above).
            cwd: Directory inside the target repository; defaults to the session's project directory.

        Returns:
            JSON with success status and enforcement_id.
        """
        try:
            from hooks.context.enforcement import add_enforcement

            if not isinstance(cadence, int) or cadence < 1:
                return json.dumps({"success": False, "error": "cadence must be int >= 1"})
            if matcher:
                from hooks.context.tool_matcher import parse

                try:
                    parse(matcher)
                except ValueError as e:
                    return json.dumps({"success": False, "error": f"invalid matcher: {e}"})
            enforcement_id = add_enforcement(
                message=message,
                cadence=cadence,
                tag=tag or None,
                matcher=matcher or None,
                local=local,
                cwd=_scope_cwd(cwd) if local else None,
            )
            if enforcement_id:
                return json.dumps(
                    {
                        "success": True,
                        "enforcement_id": enforcement_id,
                        "cadence": cadence,
                        "tag": tag or None,
                        "matcher": matcher or None,
                        "scope": "local" if local else "global",
                    }
                )
            return json.dumps({"success": False, "error": "Empty message or invalid cadence"})
        except Exception as e:
            log("MCP enforcement_set failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def enforcement_list(local: bool = False, cwd: str = "") -> str:
        """List active enforcements with their cadence and tag.

        Args:
            local: List only the current repository's entries.
            cwd: Directory inside the target repository; defaults to the session's project directory.

        Returns:
            JSON with the enforcement entries and total count.
        """
        try:
            from hooks.context.enforcement import list_enforcements

            entries = list_enforcements(local=local, cwd=_scope_cwd(cwd) if local else None)
            return json.dumps({"success": True, "enforcements": entries, "count": len(entries)})
        except Exception as e:
            log("MCP enforcement_list failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def enforcement_clear(enforcement_id: str = "", tag: str = "", local: bool = False, cwd: str = "") -> str:
        """Clear enforcement messages.

        If enforcement_id is provided: clear that specific enforcement.
        If tag is provided: clear all enforcements with that tag.
        If neither: clear ALL enforcements.

        Args:
            enforcement_id: Specific enforcement ID to clear (optional)
            tag: Tag to clear all matching enforcements (optional)
            local: Clear from the current repository's store instead of the global one.
            cwd: Directory inside the target repository; defaults to the session's project directory.

        Returns:
            JSON with success status and count of enforcements removed.
        """
        try:
            from hooks.context.enforcement import clear_enforcement

            count = clear_enforcement(
                enforcement_id=enforcement_id or None,
                tag=tag or None,
                local=local,
                cwd=_scope_cwd(cwd) if local else None,
            )
            return json.dumps({"success": True, "cleared": count})
        except Exception as e:
            log("MCP enforcement_clear failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
