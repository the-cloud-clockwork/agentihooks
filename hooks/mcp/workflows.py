"""Workflows MCP category: the loaded workflows, runs on a case, their saved state, and schedules that start them."""

import json
from collections.abc import Callable
from functools import partial
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

NESTED = "workflow_run is refused inside a worker run, so runs cannot nest"
KINDS = ("timer", "cron")


def _module(name: str) -> Any:
    """A module imported on first use, so listing the tools never loads it."""
    return import_module(name)


def _respond(call: Callable[[], Any]) -> str:
    """The call's result as JSON, or a refusal carrying its error."""
    try:
        return json.dumps(call())
    except (OSError, ValueError) as exc:
        return json.dumps({"success": False, "error": str(exc)})


def listed() -> dict[str, Any]:
    """Every workflow the current folder resolves, with its layer and file, and the load errors."""
    found, errors = _module("scripts.workflows.loader").resolve(Path.cwd())
    workflows = [{"name": name, "layer": found[name].layer, "path": str(found[name].path)} for name in sorted(found)]
    return {"success": True, "workflows": workflows, "errors": errors}


def definition(name: str) -> Any:
    """The named workflow as the current folder resolves it."""
    found, _ = _module("scripts.workflows.loader").resolve(Path.cwd())
    if name not in found:
        raise ValueError(f"no workflow named {name}")
    return found[name].workflow


def start(name: str, case: str, wait: bool) -> Any:
    """Run the named workflow on the case to its end, or hand it to a detached resume."""
    if _module("hooks.context.worker_mode").active():
        raise ValueError(NESTED)
    return _module("hooks.mcp.agents").launch(definition(name), case, wait)


def scheduled(name: str, case: str, moment: dict[str, str], timezone: str, owner: Callable[[], str]) -> dict[str, Any]:
    """Arm a cron or timer whose target starts the named workflow on the case file."""
    commands = _module("scripts.scheduler.commands")
    if sum(1 for value in moment.values() if value) != 1:
        raise commands.CommandError("give one of expression or when")
    definition(name)
    slot = commands.target_slot("workflow", name, case)
    scheduler = commands.connect()
    message = f"workflow {name}"
    if moment["expression"]:
        entry = commands.add_cron(scheduler, owner(), slot, message, expression=moment["expression"], timezone=timezone)
    else:
        entry = commands.add_timer(scheduler, owner(), slot, message, **_when(commands, moment["when"]))
    return {"entry": entry}


def _when(commands: ModuleType, text: str) -> dict[str, float]:
    """A duration such as 5m as a delay, else an ISO time with an offset as a moment."""
    try:
        return {"delay": commands.seconds(text)}
    except commands.CommandError:
        return {"at": commands.moment(text)}


def unscheduled(schedule_id: str) -> dict[str, Any]:
    """Cancel an open cron or timer whose target is a workflow."""
    commands = _module("scripts.scheduler.commands")
    scheduler = commands.connect()
    refusal = commands.CommandError(f"no workflow schedule {schedule_id}")
    try:
        record = scheduler.get(schedule_id).record
    except KeyError:
        raise refusal from None
    if record["kind"] not in KINDS or not any(slot["kind"] == "workflow" for slot in record["slots"]):
        raise refusal
    return {"entry": commands.cancel(scheduler, record["kind"], schedule_id)}


def workflow_list() -> str:
    """Every workflow loaded from the package, bundle, profile and project layers, with its layer and file."""
    return _respond(listed)


def workflow_run(name: str, case: str, wait: bool = True) -> str:
    """Run one workflow on a case; refused inside a worker run.

    Args:
        name: the workflow name, as workflow_list shows it.
        case: the case text the workflow works on.
        wait: true returns the finished run; false returns the running state at once,
            read later with workflow_status and its run id.
    """
    return _respond(lambda: start(name, case, wait))


def workflow_status(run: str) -> str:
    """The saved state of a workflow run.

    Args:
        run: the run id, as workflow_run returns it.
    """
    return _respond(lambda: _module("scripts.workflows.store").load(_module("scripts.workflows.cli").run_folder(run)))


def workflow_schedule(
    name: str,
    case: str,
    expression: str = "",
    when: str = "",
    timezone: str = "UTC",
    session_id: str = "",
    agent_name: str = "",
) -> str:
    """Start a workflow on a case file on every match of a cron expression, or once at a delay or time.

    Args:
        name: the workflow name, as workflow_list shows it.
        case: path to the case file the run reads when it fires.
        expression: five cron fields: minute hour day month weekday. Give one of expression or when.
        when: one firing, after a duration (90, 5m, 2h) or at an ISO time such as 2026-10-18T09:30:00+00:00.
        timezone: time zone the expression is read in.
        session_id: the calling session; required when the server serves many sessions and agent_name is empty.
        agent_name: the calling swarm agent's name; owns the schedule.

    Returns:
        JSON with the schedule entry and its id.
    """
    tools = _module("hooks.mcp.scheduler")
    moment = {"expression": expression, "when": when}
    caller = partial(tools.owner, session_id, agent_name)
    return tools.answer("workflow_schedule", lambda: scheduled(name, case, moment, timezone, caller))


def workflow_unschedule(schedule_id: str) -> str:
    """Cancel a workflow schedule that has not ended.

    Args:
        schedule_id: the id workflow_schedule returned.
    """
    return _module("hooks.mcp.scheduler").answer("workflow_unschedule", lambda: unscheduled(schedule_id))


TOOLS = (workflow_list, workflow_run, workflow_status, workflow_schedule, workflow_unschedule)


def register(mcp: Any) -> None:
    """Register every workflows tool."""
    for tool in TOOLS:
        mcp.tool()(tool)
