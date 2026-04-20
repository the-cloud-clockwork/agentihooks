"""Channel + brain MCP tools — publish, subscribe, and manage broadcast channels."""

import json
import re

from hooks.common import log

_VALID_CHANNEL_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


def _validate_channel(channel: str) -> str | None:
    """Return error message if channel name is invalid, None if OK."""
    if not channel or len(channel) > 64:
        return "Channel name required (max 64 chars)"
    if not _VALID_CHANNEL_NAME.match(channel):
        return "Channel name may only contain letters, digits, dots, dashes, underscores"
    return None


def register(mcp):
    @mcp.tool()
    def channel_publish(channel: str, message: str, severity: str = "info", ttl_seconds: int = 3600) -> str:
        """Publish a message to a named broadcast channel.

        Only sessions subscribed to this channel (via .agentihooks.json) will receive it.

        Args:
            channel: Channel name (e.g. "brain", "ops-alerts", "deploy-status")
            message: Message content to broadcast
            severity: "info", "alert", or "critical" (default: info)
            ttl_seconds: Time-to-live in seconds (default: 3600)

        Returns:
            JSON with success status and message_id.
        """
        try:
            from hooks.context.broadcast import create_broadcast

            msg_id = create_broadcast(
                message=message,
                severity=severity,
                ttl_seconds=ttl_seconds,
                source="mcp-channel",
                channel=channel,
            )
            if msg_id:
                return json.dumps({"success": True, "message_id": msg_id, "channel": channel})
            return json.dumps({"success": False, "error": "Empty message"})
        except Exception as e:
            log("MCP channel_publish failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def channel_list() -> str:
        """List all channels with active (non-expired) messages.

        Returns:
            JSON with channels list, message counts per channel, and global message count.
        """
        try:
            from hooks.context.broadcast import list_broadcasts

            msgs = list_broadcasts()
            channels: dict[str, int] = {}
            global_count = 0
            for m in msgs:
                ch = m.get("channel")
                if ch:
                    channels[ch] = channels.get(ch, 0) + 1
                else:
                    global_count += 1

            return json.dumps(
                {
                    "success": True,
                    "channels": channels,
                    "global_messages": global_count,
                    "total_messages": len(msgs),
                }
            )
        except Exception as e:
            log("MCP channel_list failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def channel_subscribe(channel: str) -> str:
        """Add a channel subscription to the current project's .agentihooks.json.

        Args:
            channel: Channel name to subscribe to

        Returns:
            JSON with success status and updated channels list.
        """
        try:
            from pathlib import Path

            config_path = Path.cwd() / ".agentihooks.json"
            cfg = {}
            if config_path.exists():
                cfg = json.loads(config_path.read_text())

            channels = cfg.get("channels", [])
            if not isinstance(channels, list):
                channels = []
            if channel not in channels:
                channels.append(channel)
                cfg["channels"] = channels
                config_path.write_text(json.dumps(cfg, indent=2))

            return json.dumps({"success": True, "channels": channels})
        except Exception as e:
            log("MCP channel_subscribe failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def channel_unsubscribe(channel: str) -> str:
        """Remove a channel subscription from the current project's .agentihooks.json.

        Args:
            channel: Channel name to unsubscribe from

        Returns:
            JSON with success status and updated channels list.
        """
        try:
            from pathlib import Path

            config_path = Path.cwd() / ".agentihooks.json"
            if not config_path.exists():
                return json.dumps({"success": False, "error": "No .agentihooks.json in CWD"})

            cfg = json.loads(config_path.read_text())
            channels = cfg.get("channels", [])
            if channel in channels:
                channels.remove(channel)
                cfg["channels"] = channels
                config_path.write_text(json.dumps(cfg, indent=2))

            return json.dumps({"success": True, "channels": channels})
        except Exception as e:
            log("MCP channel_unsubscribe failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def channel_acknowledge(message_id: str) -> str:
        """Acknowledge a broadcast message — stops it from re-injecting for this session.

        Use when you've processed a persistent broadcast and don't need to see it again.
        The message remains active for other sessions that haven't acknowledged it.
        The message ID is shown in broadcast banners as 'ID: <id>'.

        Args:
            message_id: The broadcast message ID to acknowledge

        Returns:
            JSON with success status.
        """
        try:
            import os

            from hooks.context.broadcast import acknowledge_broadcast

            session_id = os.getenv("CLAUDE_SESSION_ID", "unknown")
            found = acknowledge_broadcast(session_id, message_id)
            if found:
                return json.dumps({"success": True, "message_id": message_id, "acknowledged": True})
            return json.dumps({"success": False, "error": f"Message {message_id} not found"})
        except Exception as e:
            log("MCP channel_acknowledge failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def channel_clear(channel: str = "", message_id: str = "") -> str:
        """Clear broadcast messages.

        If message_id is provided: clear that specific message.
        If channel is provided: clear all messages on that channel.
        If neither: clear all broadcasts.

        Args:
            channel: Channel name to clear (optional)
            message_id: Specific message ID to clear (optional)

        Returns:
            JSON with success status and count of messages removed.
        """
        try:
            from hooks.context.broadcast import clear_broadcasts

            count = clear_broadcasts(
                message_id=message_id or None,
                channel=channel or None,
            )
            return json.dumps({"success": True, "cleared": count})
        except Exception as e:
            log("MCP channel_clear failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def brain_refresh() -> str:
        """Force the brain adapter to re-read its source and republish to the brain channel.

        Returns:
            JSON with success status and whether new content was published.
        """
        try:
            from hooks.context.brain_adapter import force_refresh

            published = force_refresh()
            return json.dumps({"success": True, "published": published})
        except Exception as e:
            log("MCP brain_refresh failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def brain_status() -> str:
        """Return current brain adapter state: source type, path, entry count, channel.

        Returns:
            JSON with brain adapter status details.
        """
        try:
            from hooks.context.brain_adapter import get_status

            status = get_status()
            return json.dumps({"success": True, **status})
        except Exception as e:
            log("MCP brain_status failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
