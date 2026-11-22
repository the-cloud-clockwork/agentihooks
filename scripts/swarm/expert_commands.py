"""The expert verb of the swarm command line: the mission template, starting and stopping a lineage, where each
lineage stands and the mission an expert reads.

Starting and stopping belong to the master of the swarm and the operator. An expert reads the mission of its own
lineage and no other.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.gates import Who
from scripts.handoff.transfers import now_ms
from scripts.swarm import clearance, expert_launch, expert_lineage, expert_mission
from scripts.swarm.store import SwarmError

VACANT = "no expert yet"


def register(sub: Any) -> None:
    """Add the expert verb and its actions to the swarm command parser."""
    actions = sub.add_parser("expert").add_subparsers(dest="action", required=True)
    actions.add_parser("template").set_defaults(handler=cmd_template)
    start = actions.add_parser("start")
    start.add_argument("mission")
    start.add_argument("--effort", default=expert_launch.DEFAULT_EFFORT)
    start.set_defaults(handler=cmd_start)
    status = actions.add_parser("status")
    status.add_argument("lineage", nargs="?", default="")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=cmd_status)
    stop = actions.add_parser("stop")
    stop.add_argument("lineage")
    stop.add_argument("--note", required=True)
    stop.set_defaults(handler=cmd_stop)
    mission = actions.add_parser("mission")
    mission.add_argument("lineage", nargs="?", default="")
    mission.set_defaults(handler=cmd_mission)


def cmd_template(store: Any, args: argparse.Namespace) -> None:
    """Print the mission template a master fills before it starts a lineage."""
    print(expert_mission.TEMPLATE, end="")


def cmd_start(store: Any, args: argparse.Namespace) -> None:
    """Save the mission file and open a running lineage for it, for the master or the operator."""
    store.config(args.slug)
    by = _starter(store, args.slug)
    path = Path(args.mission).expanduser().resolve()
    try:
        text = path.read_bytes().decode()
    except OSError as exc:
        raise SwarmError(f"cannot read the mission {args.mission}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise SwarmError(f"cannot read the mission {args.mission}: it is not UTF-8 text") from exc
    lineage = expert_lineage.start(store, args.slug, text, str(path), args.effort, by, now_ms())
    print(json.dumps(asdict(lineage)))


def cmd_status(store: Any, args: argparse.Namespace) -> None:
    """Print where each running lineage stands, or the one named, running or ended: its state, its effort, its expert
    and its mission."""
    store.config(args.slug)
    if args.lineage:
        rows = [expert_lineage.get(store, args.slug, args.lineage)]
    else:
        rows = expert_lineage.running(store, args.slug)
    agents = store.agents(args.slug)
    if args.json:
        print(json.dumps([{**asdict(row), "occupant": occupant(agents, row.id)} for row in rows]))
        return
    for row in rows:
        print(stands(row, occupant(agents, row.id)))
    if not rows:
        print(expert_lineage.NO_RUNNING)


def stands(lineage: expert_lineage.Lineage, expert: str) -> str:
    """Where one lineage stands, in one line: its state, effort, expert and mission, who started it and who ended it."""
    ended = f", ended by {lineage.ended_by}: {lineage.note}" if lineage.ended_by else ""
    return (
        f"{lineage.id} {lineage.state} at {lineage.effort}, {expert or VACANT}: {lineage.title} "
        f"(started by {lineage.started_by}{ended})"
    )


def cmd_stop(store: Any, args: argparse.Namespace) -> None:
    """End a running lineage with a note, for the master or the operator."""
    store.config(args.slug)
    ended = expert_lineage.stop(store, args.slug, args.lineage, _starter(store, args.slug), args.note, now_ms())
    print(json.dumps(asdict(ended)))


def cmd_mission(store: Any, args: argparse.Namespace) -> None:
    """Print the line that names a mission and its saved text: an expert's own, or the named one for its starter."""
    store.config(args.slug)
    lineage = expert_lineage.get(store, args.slug, _readable(store, args))
    text = expert_lineage.mission(store, args.slug, lineage)
    print(expert_mission.line(args.slug, text))
    print()
    print(text.rstrip("\n"))


def occupant(agents: list[Any], lineage: str) -> str:
    """The name of the expert that holds the lineage's seat now, empty while it has none."""
    held = (a.name for a in agents if a.lane == expert_launch.LANE and a.task == lineage and a.state != "finished")
    return next(held, "")


def _who() -> Who:
    return Who.from_env()  # pyright: ignore[reportUnknownMemberType]


def _starter(store: Any, slug: str) -> str:
    cleared: str = clearance.holder(store, slug, _who())  # pyright: ignore[reportUnknownMemberType]
    return cleared


def _readable(store: Any, args: argparse.Namespace) -> str:
    name = store.names.resolve(args.name or _who().name)
    own = next((a.task for a in store.agents(args.slug) if a.name == name and a.lane == expert_launch.LANE), "")
    if own and args.lineage in ("", own):
        return own
    if own:
        raise SwarmError(f"{name} works lineage {own} and reads the mission of no other")
    if not args.lineage:
        raise SwarmError("name the lineage whose mission to print; only an expert has one of its own")
    _starter(store, args.slug)
    return args.lineage
