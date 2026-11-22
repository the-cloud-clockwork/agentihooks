"""The expert verb of the swarm command line: the mission template, starting and stopping a lineage, where each
lineage stands, the mission an expert reads and the mission proof a lineage ends on.

Starting and stopping belong to the master of the swarm and the operator. An expert reads the mission of its own
lineage and no other, and it alone completes that lineage, on a mission proof that passes.
"""

import argparse
import contextlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from scripts.gates import Who
from scripts.handoff import check
from scripts.handoff import expert as handoff_expert
from scripts.handoff.resolve import Resolver
from scripts.handoff.transfers import now_ms
from scripts.inbox import exits, outbox
from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]
from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import clearance, expert_launch, expert_lineage, expert_mission, ledger_events
from scripts.swarm.store import MASTER, SwarmError

VACANT = "no expert yet"
ONLY = "only the expert that works a running lineage completes it, from its own session"
REFUSED = "mission proof refused; fix what it names and run the command again:"
NOTICE = (
    "Expert lineage {id} completed its mission: {title}. Its mission proof was accepted; read it with "
    "`agentihooks swarm {slug} expert proof {id}`."
)
NOTICED = "expert-proof"
RECAPPED = ("Deliverables", "Validation", "Open items")
SEATED = (
    "{name} works lineage {lineage}, which still runs: an expert leaves its seat by a handoff, "
    "`agentihooks swarm {slug} handoff <doc>`, or by a mission proof, `agentihooks swarm {slug} expert complete <proof>`"
)


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
    complete = actions.add_parser("complete")
    complete.add_argument("proof")
    complete.set_defaults(handler=cmd_complete)
    proof = actions.add_parser("proof")
    proof.add_argument("lineage")
    proof.set_defaults(handler=cmd_proof)


def cmd_template(store: Any, args: argparse.Namespace) -> None:
    """Print the mission template a master fills before it starts a lineage."""
    print(expert_mission.TEMPLATE, end="")


def cmd_start(store: Any, args: argparse.Namespace) -> None:
    """Save the mission file and open a running lineage for it, for the master or the operator."""
    store.config(args.slug)
    by = _starter(store, args.slug)
    path, text = _document(args.mission, "mission")
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
    """Where one lineage stands, in one line: its state, effort and mission, the expert on its seat while it runs,
    who started it and who ended it."""
    seat = f", {expert or VACANT}" if lineage.state == expert_lineage.RUNNING else ""
    ended = f", ended by {lineage.ended_by}: {lineage.note}" if lineage.ended_by else ""
    return (
        f"{lineage.id} {lineage.state} at {lineage.effort}{seat}: {lineage.title} "
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


def cmd_complete(store: Any, args: argparse.Namespace) -> None:
    """End the lineage the calling expert works, on a mission proof that passes; a faulty proof changes nothing. Run
    again with the proof the lineage ended on, it finishes whatever a failure cut short after that end."""
    store.config(args.slug)
    agent = _occupant(store, args.slug)
    lineage = expert_lineage.get(store, args.slug, agent.task)
    _, text = _document(args.proof, "mission proof")
    mission = expert_lineage.mission(store, args.slug, lineage)
    found = handoff_expert.proof_problems(text, Resolver(args.slug, store.redis), _merged, args.slug, mission)
    if found:
        raise SwarmError(REFUSED + "".join(f"\n- {fault}" for fault in found))
    at = now_ms()
    kept = expert_lineage.proof(store, args.slug, lineage.id) == text
    ended = lineage if kept else expert_lineage.complete(store, args.slug, lineage.id, agent.name, text, at)
    _close(store, args.slug, agent, ended, text)
    print(json.dumps(asdict(ended)))


def _close(store: Any, slug: str, agent: Any, ended: expert_lineage.Lineage, text: str) -> None:
    """Tell the master, keep the recap, settle the expert's mail and finish the expert, in that order. Each step can
    run again: the notice is one inbox item under an id fixed by the lineage, the recap is kept once, and the expert
    is finished last, so the command still finds it when a failure cut the steps short."""
    inbox = InboxStore(store.redis)
    notice = NOTICE.format(slug=slug, id=ended.id, title=ended.title)
    with contextlib.suppress(DuplicateItem):
        inbox.send(agent.name, seat_address(slug, MASTER), notice, item_id=outbox.stable_id(NOTICED, slug, ended.id))
    recap = "\n\n".join(f"## {heading}\n{check.section(text, heading)}" for heading in RECAPPED)
    kept: list[dict[str, Any]] = store.memory.recaps(agent.seat)
    if not kept or kept[0]["text"] != recap:
        store.memory.add_recap(agent.seat, agent.name, agent.task, recap, ended.ended_at)
    exits.settle(inbox, agent.name, "", "completed its mission and left")  # pyright: ignore[reportUnknownMemberType]
    store.put_agent(slug, replace(agent, state="finished"))


def cmd_proof(store: Any, args: argparse.Namespace) -> None:
    """Print the mission proof a lineage ended on, for the master or the operator."""
    store.config(args.slug)
    _starter(store, args.slug)
    lineage = expert_lineage.get(store, args.slug, args.lineage)
    text = expert_lineage.proof(store, args.slug, lineage.id)
    if not text:
        raise SwarmError(f"expert lineage {lineage.id} holds no mission proof")
    print(text, end="")


def _document(named: str, what: str) -> tuple[Path, str]:
    """The resolved path and the text of a document an agent names; one that cannot be read is refused by name."""
    path = Path(named).expanduser().resolve()
    try:
        return path, path.read_bytes().decode()
    except OSError as exc:
        raise SwarmError(f"cannot read the {what} {named}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise SwarmError(f"cannot read the {what} {named}: it is not UTF-8 text") from exc


def _occupant(store: Any, slug: str) -> Any:
    """The calling expert, by the identity of its own session, while it holds the seat of its lineage; anyone else,
    and anyone who only names it, is refused."""
    name = store.names.resolve(_who().name)
    held = (
        agent
        for agent in store.agents(slug)
        if agent.name == name and agent.lane == expert_launch.LANE and agent.state != "finished"
    )
    agent = next(held, None)
    if agent is None:
        raise SwarmError(ONLY)
    return agent


def _merged(url: str) -> bool:
    """Whether the pull request is merged; one that cannot be read counts as not merged."""
    pull = ledger_events.view(url)
    return pull is not None and pull.state == "MERGED"


def occupant(agents: list[Any], lineage: str) -> str:
    """The name of the expert that holds the lineage's seat now, empty while it has none."""
    held = (a.name for a in agents if a.lane == expert_launch.LANE and a.task == lineage and a.state != "finished")
    return next(held, "")


def seated(store: Any, slug: str, agent: Any) -> None:
    """Refuse an expert that would leave its seat while its lineage runs, or after it ended on a proof whose
    completion did not finish; anyone else passes."""
    if agent is None or agent.lane != expert_launch.LANE or agent.state == "finished":
        return
    try:
        lineage = expert_lineage.get(store, slug, agent.task)
    except SwarmError:
        return
    if lineage.state == expert_lineage.RUNNING:
        raise SwarmError(SEATED.format(name=agent.name, lineage=lineage.id, slug=slug))
    if lineage.state == expert_lineage.COMPLETE:
        raise SwarmError(expert_lineage.UNFINISHED.format(lineage=lineage.id, slug=slug))


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
