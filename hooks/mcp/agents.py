"""Agents MCP category: the profile cards, and one profile run as a one-stage workflow through the runner."""

import json
import subprocess
import sys
from collections.abc import Callable
from importlib import import_module
from typing import Any
from uuid import uuid4

STAGE = "run"
NESTED = "agent_run is refused inside a worker run, so runs cannot nest"


def _module(name: str) -> Any:
    """A module imported on first use, so listing the tools never loads it."""
    return import_module(name)


def _respond(call: Callable[[], Any]) -> str:
    """The call's result as JSON, or a refusal carrying its error."""
    try:
        return json.dumps(call())
    except (OSError, ValueError) as exc:
        return json.dumps({"success": False, "error": str(exc)})


def one_stage(profile: str) -> Any:
    """A workflow whose one stage runs the profile on the case; its pass exits and any other outcome exhausts it."""
    return _module("scripts.workflows.schema").validate(
        {
            "workflow": {
                "name": f"agent-{profile}",
                "intent": f"Run the {profile} profile on the input",
                "head": STAGE,
                "exit": f"{STAGE}.pass",
                "max_iterations": 1,
            },
            "stages": {STAGE: {"profile": profile, "next": {"pass": "exit", "retry": "exit", "enhance": "exit"}}},
        }
    )


def detach(run: str) -> None:
    """Resume the saved run in its own session, so it outlives this server."""
    subprocess.Popen(
        [sys.executable, "-m", "scripts.install", "workflow", "resume", run],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launch(definition: Any, case: str, wait: bool) -> Any:
    """Save a run of the definition on the case, then run it to its end or hand it to a detached resume."""
    cli = _module("scripts.workflows.cli")
    store = _module("scripts.workflows.store")
    folder = cli.run_folder(uuid4().hex)
    state = store.create(folder, definition, case)
    state.update(definition=definition.to_document(), harness="auto")
    store.save(folder, state)
    if wait:
        return cli.execute(folder, state)
    detach(folder.name)
    return state


def start(profile: str, case: str, wait: bool) -> Any:
    """Save a one-stage run of the profile, then run it to its end or hand it to a detached resume."""
    if _module("hooks.context.worker_mode").active():
        raise ValueError(NESTED)
    _module("scripts.agents.cli").show_card(profile)
    return launch(one_stage(profile), case, wait)


def agent_list(capability: str = "") -> str:
    """Every agent profile card: name, description and capabilities.

    Args:
        capability: only cards declaring this capability; empty lists every card.
    """
    return _respond(lambda: _module("scripts.agents.cli").list_cards(capability or None))


def agent_show(name: str) -> str:
    """One agent profile card.

    Args:
        name: the profile name, as agent_list shows it.
    """
    return _respond(lambda: _module("scripts.agents.cli").show_card(name))


def agent_run(profile: str, input: str, wait: bool = True) -> str:
    """Run one agent profile on an input as a one-stage workflow; refused inside a worker run.

    Args:
        profile: the profile name, as agent_list shows it.
        input: the case text the profile works on.
        wait: true returns the finished run; false returns the running state at once,
            read later with agentihooks workflow status and its run id.
    """
    return _respond(lambda: start(profile, input, wait))


def register(mcp: Any) -> None:
    """Register agent_list, agent_show and agent_run."""
    for tool in (agent_list, agent_show, agent_run):
        mcp.tool()(tool)
