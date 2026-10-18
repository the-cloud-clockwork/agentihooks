"""Scheduler MCP tools — timers, stopwatches and crons that deliver a message to a target.

A target is fleet (a broadcast), swarm (one inbox item per live agent of the swarm named by name),
seat (the seat address in name) or session (the session name in name). Ids come from the scheduler.
"""

import json

from hooks.common import log
from hooks.mcp._session import resolve_session_id


def _owner(session_id: str) -> str:
    from scripts.scheduler.commands import CommandError

    owner = resolve_session_id(session_id)
    if not owner:
        raise CommandError("pass session_id: this server cannot tell which session is calling")
    return owner


def _answer(tool: str, action) -> str:
    try:
        return json.dumps({"success": True, **action()})
    except Exception as e:
        log(f"MCP {tool} failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


def _listed(kind: str) -> dict:
    from scripts.scheduler import commands

    found = commands.entries(commands.connect(), kind)
    return {"entries": found, "count": len(found)}


def register(mcp):
    from scripts.scheduler import commands

    @mcp.tool()
    def timer_add(
        message: str, delay: str = "", at: str = "", target: str = "fleet", name: str = "", session_id: str = ""
    ) -> str:
        """Fire message once at a target, after delay or at a time.

        Args:
            message: Text delivered when the timer fires.
            delay: Seconds, or a number with s, m, h or d (90, 5m, 2h). Give delay or at.
            at: ISO time such as 2026-10-18T09:30:00+00:00. Give delay or at.
            target: fleet, swarm, seat or session.
            name: Swarm slug, seat address or session name; empty for fleet.
            session_id: The calling session; required when the server serves many sessions.

        Returns:
            JSON with the timer entry and its id.
        """

        def action():
            if bool(delay) == bool(at):
                raise commands.CommandError("give delay or at")
            when = {"at": commands.moment(at)} if at else {"delay": commands.seconds(delay)}
            slot = commands.target_slot(target, name)
            return {"entry": commands.add_timer(commands.connect(), _owner(session_id), slot, message, **when)}

        return _answer("timer_add", action)

    @mcp.tool()
    def timer_list() -> str:
        """List timers still waiting to fire on this machine, soonest first."""
        return _answer("timer_list", lambda: _listed("timer"))

    @mcp.tool()
    def timer_cancel(timer_id: str) -> str:
        """Cancel a timer that has not fired."""
        return _answer("timer_cancel", lambda: {"entry": commands.cancel(commands.connect(), "timer", timer_id)})

    @mcp.tool()
    def stopwatch_start(message: str, limit: str, target: str = "fleet", name: str = "", session_id: str = "") -> str:
        """Start a stopwatch that delivers message to a target when its elapsed time crosses limit.

        Args:
            message: Text delivered when the limit is reached.
            limit: Seconds, or a number with s, m, h or d.
            target: fleet, swarm, seat or session.
            name: Swarm slug, seat address or session name; empty for fleet.
            session_id: The calling session; required when the server serves many sessions.

        Returns:
            JSON with the stopwatch entry and its id.
        """

        def action():
            slot = commands.target_slot(target, name)
            entry = commands.start_stopwatch(
                commands.connect(), _owner(session_id), slot, message, limit=commands.seconds(limit)
            )
            return {"entry": entry}

        return _answer("stopwatch_start", action)

    @mcp.tool()
    def stopwatch_stop(stopwatch_id: str) -> str:
        """Stop a running stopwatch and report its elapsed seconds."""
        return _answer("stopwatch_stop", lambda: {"entry": commands.stop_stopwatch(commands.connect(), stopwatch_id)})

    @mcp.tool()
    def stopwatch_show(stopwatch_id: str) -> str:
        """Show a stopwatch with its state and elapsed seconds."""
        return _answer("stopwatch_show", lambda: {"entry": commands.show_stopwatch(commands.connect(), stopwatch_id)})

    @mcp.tool()
    def cron_add(
        message: str,
        expression: str,
        target: str = "fleet",
        name: str = "",
        timezone: str = "UTC",
        session_id: str = "",
    ) -> str:
        """Deliver message to a target on every match of a five field cron expression.

        Args:
            message: Text delivered on each firing.
            expression: Five fields: minute hour day month weekday.
            target: fleet, swarm, seat or session.
            name: Swarm slug, seat address or session name; empty for fleet.
            timezone: Time zone the expression is read in.
            session_id: The calling session; required when the server serves many sessions.

        Returns:
            JSON with the cron entry and its id.
        """

        def action():
            slot = commands.target_slot(target, name)
            entry = commands.add_cron(
                commands.connect(), _owner(session_id), slot, message, expression=expression, timezone=timezone
            )
            return {"entry": entry}

        return _answer("cron_add", action)

    @mcp.tool()
    def cron_list() -> str:
        """List cron schedules on this machine, next firing first."""
        return _answer("cron_list", lambda: _listed("cron"))

    @mcp.tool()
    def cron_remove(cron_id: str) -> str:
        """Remove a cron schedule."""
        return _answer("cron_remove", lambda: {"entry": commands.cancel(commands.connect(), "cron", cron_id)})
