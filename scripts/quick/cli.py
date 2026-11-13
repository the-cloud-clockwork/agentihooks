"""agentihooks quick: start, inspect, message, hand off and stop a quick swarm."""

import argparse
import dataclasses
import json
import os
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from scripts.quick import guard, handoff, launch, layout, member, paths, registry, room, tick
from scripts.quick.guard import QuickError
from scripts.swarm import safety_nudge

NOT_MEMBER = "this session is not a member of a quick swarm: run it from a quick swarm pane"


def parser() -> argparse.ArgumentParser:
    """The agentihooks quick command line."""
    root = argparse.ArgumentParser(prog="agentihooks quick", description="A herdr-only master and engineers")
    sub = root.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="Split this herdr pane's tab and launch the engineers; this session is master")
    up.add_argument("--agents", type=int, default=layout.DEFAULT_AGENTS, help="Agents with the master, 2 to 5")
    up.add_argument("--harness", default="claude", help="Engineer harness, or one per engineer: claude,codex")
    up.add_argument("--dir", default="", help="Directory the engineers work in; default the current one")
    sub.add_parser("status", help="Print every seat of this session's quick swarm")
    given = sub.add_parser("handoff", help="Accept this seat's Handoff v2 and start its successor in this pane")
    given.add_argument("document", nargs="?", default="", help="Handoff v2 file; default the seat's handoff")
    again = sub.add_parser("relaunch", help="Start the successor of a seat that handed off")
    again.add_argument("seat")
    again.add_argument("--folder", required=True)
    send = sub.add_parser("send", help="Post a message to another seat through the room")
    send.add_argument("to")
    send.add_argument("text")
    sub.add_parser("room", help="Print the room messages sent to this seat since it last read")
    sub.add_parser("tick", help="Run one quick tick over every live quick swarm")
    sub.add_parser("stop", help="Stop this session's quick swarm, keeping its seat folders")
    return root


def _harnesses(value: str, engineers: int) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if len(names) == 1:
        return names * engineers
    if len(names) != engineers:
        raise QuickError(f"--harness names {len(names)} harnesses for {engineers} engineers")
    return names


def _swarm_for(
    args: argparse.Namespace, environ: dict[str, str], caller: str, quick_id: str, directory: Path
) -> registry.Swarm:
    names = layout.seats(args.agents)
    harnesses = _harnesses(args.harness, len(names) - 1)
    panes = layout.build(caller, args.agents, layout.HerdrPanes(directory, environ))
    harness = environ.get("AGENTIHOOKS_TARGET") or "claude"
    session = environ.get("CLAUDE_CODE_SESSION_ID", "")
    master = registry.Seat(1, names[0], caller, harness, f"{quick_id}-{names[0]}", session_id=session)
    engineers = [
        registry.Seat(position, name, pane, harness, f"{quick_id}-{name}")
        for position, (name, pane, harness) in enumerate(zip(names[1:], panes[1:], harnesses, strict=True), 2)
    ]
    return registry.Swarm(quick_id, str(directory), (master, *engineers))


def _up(args: argparse.Namespace, environ: dict[str, str]) -> int:
    caller = guard.require(environ)
    directory = Path(args.dir or os.getcwd()).resolve()
    quick_id = paths.quick_id(environ)
    folder = paths.folder_for(environ, quick_id, directory)
    with paths.host_locked(environ):
        if registry.load(folder) is not None:
            raise QuickError(f"quick swarm {quick_id} is already up; see agentihooks quick status")
        try:
            safety_nudge.install(environ)
        except RuntimeError as error:
            raise QuickError(str(error)) from error
        swarm = _swarm_for(args, environ, caller, quick_id, directory)
        paths.create(folder, [seat.seat for seat in swarm.seats])
        registry.save(folder, swarm)
        paths.register(environ, quick_id, folder)
    started = launch.start_all(folder, swarm, environ)
    exited = launch.exited(swarm.seats[1:], tick.LiveHerdr(environ).bare)
    failed = sorted({name for name, code in started.items() if code != 0} | set(exited))
    for name in failed:
        registry.set_state(folder, name, registry.PENDING)
    print(json.dumps({"quick_id": quick_id, "folder": str(folder), "started": started}))
    for seat in swarm.seats:
        print(json.dumps(dataclasses.asdict(seat)))
    if failed:
        print(f"agentihooks quick: {', '.join(failed)} did not start; the next round retries it", file=sys.stderr)
    return 1 if failed else 0


def _member(environ: Mapping[str, str]) -> member.Member:
    found = member.find(environ)
    if found is None:
        raise QuickError(NOT_MEMBER)
    return found


def _status(environ: Mapping[str, str]) -> int:
    for seat in _member(environ).swarm.seats:
        print(json.dumps(dataclasses.asdict(seat)))
    return 0


def _handoff(document: str, environ: Mapping[str, str]) -> int:
    found = _member(environ)
    problems = handoff.accept(found, Path(document).expanduser() if document else None)
    if problems:
        print("handoff refused; fix the document and run the command again:", file=sys.stderr)
        print("\n".join(f"- {problem}" for problem in problems), file=sys.stderr)
        return 1
    print(f"handoff accepted: this session ends at its next stop and its successor starts in {found.seat.pane_id}")
    return 0


def _relaunch(seat: str, folder: str, environ: dict[str, str]) -> int:
    live = tick.LiveHerdr(environ)
    outcome = launch.relaunch(Path(folder), seat, environ, bare=live.bare)
    print(outcome)
    return 0 if outcome == "started" else 1


def _send(to: str, text: str, environ: Mapping[str, str]) -> int:
    found = _member(environ)
    target = found.swarm.seat(to)
    if target is None:
        raise QuickError(f"quick swarm {found.swarm.quick_id} has no seat {to}")
    room.post(found.folder, found.seat.seat, to, text, int(time.time()))
    print(f"posted to {to}: {tick.tell(found.folder, target, tick.LiveHerdr(dict(environ)))}")
    return 0


def _room(environ: Mapping[str, str]) -> int:
    found = _member(environ)
    for line in room.unread(found.folder, found.seat.seat):
        print(f"{line['from']} -> {line['to']}: {line['text']}")
    return 0


def _stop(environ: dict[str, str]) -> int:
    found = _member(environ)
    with paths.host_locked(environ):
        registry.path(found.folder).unlink(missing_ok=True)
        shutil.rmtree(paths.room(found.folder).parent, ignore_errors=True)
        paths.unregister(environ, found.swarm.quick_id)
    print(f"stopped quick swarm {found.swarm.quick_id}; seat folders stay in {found.folder}")
    return 0


def _run(args: argparse.Namespace, environ: dict[str, str]) -> int:
    if args.command == "up":
        return _up(args, environ)
    if args.command == "handoff":
        return _handoff(args.document, environ)
    if args.command == "relaunch":
        return _relaunch(args.seat, args.folder, environ)
    if args.command == "send":
        return _send(args.to, args.text, environ)
    if args.command == "tick":
        print(json.dumps(tick.run_round(environ)))
        return 0
    commands = {"status": _status, "room": _room, "stop": _stop}
    return commands[args.command](environ)


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """agentihooks quick up|status|handoff|relaunch|send|room|tick|stop."""
    args = parser().parse_args(argv)
    try:
        return _run(args, dict(os.environ if environ is None else environ))
    except QuickError as error:
        print(f"agentihooks quick: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
