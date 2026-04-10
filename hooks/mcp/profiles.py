"""Profile overlay MCP tools — list, add, remove, refresh overlays mid-session."""

import json

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def profile_list() -> str:
        """List all available profiles (built-in + bundle) with descriptions.

        Returns:
            JSON with profiles list and current active profile info.
        """
        try:
            from pathlib import Path

            from hooks.config import AGENTIHOOKS_HOME

            state_file = Path(AGENTIHOOKS_HOME) / "state.json"
            current_profile = ""
            if state_file.exists():
                state = json.loads(state_file.read_text())
                current_profile = state.get("targets", {}).get("global", {}).get("profile", "")

            from scripts.overlay import _resolve_base_profile_dir, overlay_list

            base_dir = _resolve_base_profile_dir()
            allowed = overlay_list(base_dir)

            return json.dumps({
                "success": True,
                "current_profile": current_profile,
                "allowed_overlays": allowed,
            })
        except Exception as e:
            log("MCP profile_list failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def profile_current() -> str:
        """Return the current base profile and any active overlays.

        Returns:
            JSON with base_profile, active_overlays, and effective_chain.
        """
        try:
            from pathlib import Path

            from hooks.config import AGENTIHOOKS_HOME

            state_file = Path(AGENTIHOOKS_HOME) / "state.json"
            current_profile = ""
            if state_file.exists():
                state = json.loads(state_file.read_text())
                current_profile = state.get("targets", {}).get("global", {}).get("profile", "")

            from scripts.overlay import get_active_overlays

            overlays = get_active_overlays()
            overlay_names = [o.get("name", "") for o in overlays]
            chain = [current_profile] + overlay_names if current_profile else overlay_names

            return json.dumps({
                "success": True,
                "base_profile": current_profile,
                "active_overlays": [
                    {"name": o.get("name"), "added_at": o.get("added_at"), "added_by": o.get("added_by")}
                    for o in overlays
                ],
                "effective_chain": chain,
            })
        except Exception as e:
            log("MCP profile_current failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def overlay_add(name: str) -> str:
        """Add a profile overlay on top of the current base profile.

        The overlay's CLAUDE.md and rules will be injected into subsequent
        turns via the hook system. Takes effect on the NEXT user turn.

        Args:
            name: Profile name to overlay (must be in base profile's allowedOverlays)

        Returns:
            JSON with success status and overlay details.
        """
        try:
            from scripts.overlay import overlay_add as _add

            result = _add(name, added_by="agent")
            return json.dumps(result, default=str)
        except Exception as e:
            log("MCP overlay_add failed", {"name": name, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def overlay_remove(name: str) -> str:
        """Remove an active profile overlay.

        The overlay's rules will stop being injected on the next turn.

        Args:
            name: Profile overlay name to remove

        Returns:
            JSON with success status.
        """
        try:
            from scripts.overlay import overlay_remove as _remove

            result = _remove(name)
            return json.dumps(result)
        except Exception as e:
            log("MCP overlay_remove failed", {"name": name, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def overlay_refresh(name: str) -> str:
        """Re-render an active overlay from disk (if the bundle changed).

        Args:
            name: Profile overlay name to refresh

        Returns:
            JSON with success status and updated overlay details.
        """
        try:
            from scripts.overlay import overlay_refresh as _refresh

            result = _refresh(name)
            return json.dumps(result, default=str)
        except Exception as e:
            log("MCP overlay_refresh failed", {"name": name, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
