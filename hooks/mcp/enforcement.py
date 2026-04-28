"""Enforcement MCP tools — operator-curated drumbeat reminders.

Mirrors the channel_* tool surface but for the enforcement system:
- enforcement_set: register a message that re-injects every N tool calls
- enforcement_list: list active enforcements
- enforcement_clear: clear by id, by tag, or all
"""

import json

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def enforcement_set(message: str, cadence: int, tag: str = "") -> str:
        """Register a drumbeat enforcement that re-injects every N tool calls.

        Enforcements are global (every session sees them) and permanent until
        cleared. No severity, no TTL — just a recurring reminder. Cheap because
        re-injection only adds context tokens, no external API calls.

        Args:
            message: Reminder text to inject (e.g. "patches forbidden — code only")
            cadence: Re-inject every N tool calls. Required, must be >= 1.
            tag: Optional tag for grouping (lets you clear-by-tag later).

        Returns:
            JSON with success status and enforcement_id.
        """
        try:
            from hooks.context.enforcement import add_enforcement

            if not isinstance(cadence, int) or cadence < 1:
                return json.dumps({"success": False, "error": "cadence must be int >= 1"})
            enforcement_id = add_enforcement(
                message=message,
                cadence=cadence,
                tag=tag or None,
            )
            if enforcement_id:
                return json.dumps(
                    {
                        "success": True,
                        "enforcement_id": enforcement_id,
                        "cadence": cadence,
                        "tag": tag or None,
                    }
                )
            return json.dumps({"success": False, "error": "Empty message or invalid cadence"})
        except Exception as e:
            log("MCP enforcement_set failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def enforcement_list() -> str:
        """List all active enforcements with their cadence and tag.

        Returns:
            JSON with the enforcement entries and total count.
        """
        try:
            from hooks.context.enforcement import list_enforcements

            entries = list_enforcements()
            return json.dumps({"success": True, "enforcements": entries, "count": len(entries)})
        except Exception as e:
            log("MCP enforcement_list failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def enforcement_clear(enforcement_id: str = "", tag: str = "") -> str:
        """Clear enforcement messages.

        If enforcement_id is provided: clear that specific enforcement.
        If tag is provided: clear all enforcements with that tag.
        If neither: clear ALL enforcements.

        Args:
            enforcement_id: Specific enforcement ID to clear (optional)
            tag: Tag to clear all matching enforcements (optional)

        Returns:
            JSON with success status and count of enforcements removed.
        """
        try:
            from hooks.context.enforcement import clear_enforcement

            count = clear_enforcement(
                enforcement_id=enforcement_id or None,
                tag=tag or None,
            )
            return json.dumps({"success": True, "cleared": count})
        except Exception as e:
            log("MCP enforcement_clear failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
