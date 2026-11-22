"""The seats of the expert lineages, kept by one pass of the tick.

An expert claims no ledger task, so nothing else places it. For each running lineage this pass launches one expert
on the lineage's seat, follows an accepted handoff with the next expert on the same seat, leaves the next expert a
handoff when the last one was lost without one, runs the closing steps a completed lineage is still owed and
finishes the expert of a lineage that ended. The expert verb of the command line calls the same closing steps.
"""

import contextlib
from collections.abc import Callable
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from scripts.handoff import check, envelope, transfers
from scripts.inbox import exits, outbox
from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]
from scripts.inbox.store import DuplicateItem, InboxError, InboxStore
from scripts.swarm import dispatch_seat, expert_launch, expert_lineage, expert_mission, seat_spawn
from scripts.swarm.store import MASTER, AgentRecord, SwarmError

LANE = expert_launch.LANE
HELD = ("paused", "stopping", "stopped")
REASON = "exit"
GONE = "unknown"
FAULT = "lineage {lineage} hit a fault this pass: {error}"
NOTICE = (
    "Expert lineage {id} completed its mission: {title}. Its mission proof was accepted; read it with "
    "`agentihooks swarm {slug} expert proof {id}`."
)
NOTICED = "expert-proof"
RECAPPED = ("Deliverables", "Validation", "Open items")
COMPLETED = "completed its mission and left"
LEFT = "left when its lineage {lineage} ended"
SWARM = "swarm"
RETURNED = "expert lineage {id} is {state} and no expert holds its seat any more"
WHAT = "expert of lineage {lineage}"
SPAWNED = "spawned expert {name} on lineage {lineage} as its expert number {number}"
WAITS = "lineage {lineage} waits: {error}"
FAILED = "expert spawn failed on lineage {lineage}: {error}"
ENDED = "ended expert {name}: lineage {lineage} is {state}"
STOPPED = "left when its lineage {lineage} was stopped"
OFFERED = "offered the handoff of lineage {lineage} again: {name} was lost before it confirmed it"
RECOVERED = "wrote the recovery handoff of lineage {lineage}: {name} was lost and left none"
PENDING = "- D{number} unknown: {name} left no account of it; check what is merged and deployed before you rely on it."
MISSION = "- {address} the mission every expert of this lineage works"
RECAP = "- recap:{seat} the last state an expert of this lineage handed over"
RECOVERY = """# Expert handoff
## Mission
{line}
## Done
None
## Pending
{pending}
## Assumptions
None
## Tests
None
## Validation
None
## Stopped at
unknown: {name} held this seat and ended without a handoff, so nothing it did after its launch is recorded here.
## Decisions and promises
unknown: none are recorded outside the seat recap and the open mail of this seat.
## Next
- Read the mission, then establish what each deliverable still needs.
## Read first
{read}
<!-- handoff complete -->
"""
SEAT: Any = SimpleNamespace(
    dispatch_seat=dispatch_seat, envelope=envelope, exits=exits, seat_spawn=seat_spawn, transfers=transfers
)


def run(slug: str, config: Any, store: Any, runtime: Any, now_ms: int) -> list[str]:
    """One pass over the expert seats of a swarm: the lines of what it did. A completed lineage gets the closing
    steps it is still owed and the expert of an ended lineage is finished, whatever the swarm's state; nothing
    launches while the swarm is paused, stopping or stopped, and a seat that holds a record in any state gets no
    second expert. Each lineage is served on its own: a fault in one becomes a line and the others go on."""
    owed = expert_lineage.closing(store, slug)
    actions: list[str] = []
    for lineage in owed:
        actions += _guarded(lineage.id, _closed, slug, store, lineage)
    experts: list[AgentRecord] = [agent for agent in store.agents(slug) if agent.lane == LANE]
    lineages = expert_lineage.running(store, slug)
    kept = {lineage.id for lineage in (*lineages, *owed)}
    for agent in experts:
        if agent.state != "finished" and agent.task not in kept:
            actions += _guarded(agent.task, _end, slug, store, agent)
    if config.state in HELD:
        return actions
    seated = {agent.task for agent in experts}
    for lineage in lineages:
        if lineage.id not in seated:
            actions += _guarded(lineage.id, _launch, slug, config, store, runtime, lineage, now_ms)
    return actions


def _guarded(lineage: str, serve: Callable[..., list[str]], *given: Any) -> list[str]:
    """Serve one lineage; a refusal of the swarm store or the inbox becomes a line that names the lineage, so its
    fault never stops the pass for the others. A store that stops answering is raised: it fails every lineage."""
    try:
        return serve(*given)
    except (SwarmError, InboxError) as fault:
        return [FAULT.format(lineage=lineage, error=fault)]


def _closed(slug: str, store: Any, lineage: expert_lineage.Lineage) -> list[str]:
    """Run the closing steps a completed lineage is still owed, from the proof it kept. They need no expert record:
    the reap may have dropped a dead expert before this pass."""
    close(store, slug, lineage, expert_lineage.proof(store, slug, lineage.id))
    return [ENDED.format(name=lineage.ended_by, lineage=lineage.id, state=lineage.state)]


def _end(slug: str, store: Any, agent: AgentRecord) -> list[str]:
    """Finish the unfinished expert of a lineage that no longer runs: its open mail is withdrawn with the reason,
    the handoff left on its seat is cleared and the mail still open on that seat goes back to the master. An expert
    of a lineage the swarm does not hold ends the same way, so one stray record never stops the pass."""
    try:
        state: str = expert_lineage.get(store, slug, agent.task).state
    except SwarmError:
        state = GONE
    reason = STOPPED if state == expert_lineage.STOPPED else LEFT
    SEAT.exits.settle(InboxStore(store.redis), agent.name, "", reason.format(lineage=agent.task))
    store.clear_handoff(slug, agent.task)
    returned(store, slug, agent.seat, agent.task, state)
    store.put_agent(slug, replace(agent, state="finished"))
    return [ENDED.format(name=agent.name, lineage=agent.task, state=state)]


def close(store: Any, slug: str, ended: expert_lineage.Lineage, text: str) -> None:
    """The closing steps of a lineage that ended on its mission proof: tell the master, keep the recap, settle the
    mail of the expert that completed it, clear the handoff left on the seat and finish that expert. Each step can
    run again: the notice is one inbox item under an id fixed by the lineage, the recap is kept once, and the
    lineage stops owing its closing steps last, so the command and the expert seats pass both finish what a failure
    cut short."""
    inbox = InboxStore(store.redis)
    notice = NOTICE.format(slug=slug, id=ended.id, title=ended.title)
    with contextlib.suppress(DuplicateItem):
        inbox.send(
            ended.ended_by, seat_address(slug, MASTER), notice, item_id=outbox.stable_id(NOTICED, slug, ended.id)
        )
    recap = "\n\n".join(f"## {heading}\n{check.section(text, heading)}" for heading in RECAPPED)
    kept: list[dict[str, Any]] = store.memory.recaps(ended.seat)
    if not kept or kept[0]["text"] != recap:
        store.memory.add_recap(ended.seat, ended.ended_by, ended.id, recap, ended.ended_at)
    SEAT.exits.settle(inbox, ended.ended_by, "", COMPLETED)
    store.clear_handoff(slug, ended.id)
    returned(store, slug, ended.seat, ended.id, ended.state)
    agents: list[AgentRecord] = store.agents(slug)
    for agent in agents:
        if agent.name == ended.ended_by and agent.state != "finished":
            store.put_agent(slug, replace(agent, state="finished"))
    expert_lineage.closed(store, slug, ended.id)


def drop_confirmed(store: Any, slug: str, agent: AgentRecord) -> None:
    """Drop the stored handoff of a lineage once its expert confirmed it: from then on an expert that is lost left
    no handoff, and the next one gets a recovery handoff, not the one already taken. Any other agent's stays."""
    if agent.lane == LANE:
        store.clear_handoff(slug, agent.task)


def keep_handoff(
    store: Any, slug: str, lineage: expert_lineage.Lineage, author: AgentRecord, reason: str, text: str, at: int
) -> None:
    """Store a handoff on the seat of a lineage with the runtime's envelope, and record the transfer the next
    expert of the lineage attaches and confirms."""
    built: dict[str, Any] = SEAT.envelope.build(store, slug, author, reason, None, at)
    store.put_handoff(slug, lineage.id, text, seat=lineage.seat, envelope=built)
    SEAT.transfers.record(store, slug, author, reason, text, at)


def returned(store: Any, slug: str, seat: str, lineage: str, state: str) -> None:
    """Send the mail still open on the seat of a lineage that ended to the master's seat with the reason, so no
    message waits on a seat nobody will hold again."""
    inbox = InboxStore(store.redis)
    for item in inbox.open_items(seat):
        reason = RETURNED.format(id=lineage, state=state)
        inbox.redirect(item.id, SWARM, seat_address(slug, MASTER), reason, expected_address=seat)


def _launch(
    slug: str, config: Any, store: Any, runtime: Any, lineage: expert_lineage.Lineage, now_ms: int
) -> list[str]:
    """Launch the next expert of a running lineage on its empty seat, behind the holds every seat waits on. Its task
    carries the lineage, the mission, its number in the lineage, the stored handoff with the transfer it attaches,
    and the launch an expert is held to. The expert is counted just before its spawn and the count taken back when
    the spawn fails, so a failure after the spawn never leaves a running expert uncounted."""
    tick: Any = import_module("scripts.swarm.tick")
    what = WHAT.format(lineage=lineage.id)
    held: str = (
        SEAT.seat_spawn.placed_elsewhere(runtime, LANE, {"id": lineage.id})
        or SEAT.seat_spawn.no_slot(config, runtime, what)
        or SEAT.seat_spawn.host_hold(slug, store, now_ms, what)
    )
    if held:
        return [held]
    number = expert_lineage.launches(store, slug, lineage.id) + 1
    left = _left(slug, store, lineage, number, now_ms)
    counted: list[int] = []

    def prepare(record: AgentRecord) -> dict[str, Any]:
        task: dict[str, Any] = {
            "id": lineage.id,
            "title": lineage.title,
            "mission": lineage.mission,
            "generation": number,
            "handoff": store.handoff(slug, lineage.id),
            "transfer": SEAT.transfers.attach(store, slug, record),
            "requires_harness": expert_launch.HARNESS,
            expert_launch.EFFORT_KEY: lineage.effort,
        }
        primed: dict[str, Any] = tick.primed(store, slug, record.seat, task)
        counted.append(expert_lineage.launched(store, slug, lineage.id))
        return primed

    seat = SEAT.seat_spawn.Seat(LANE, lineage.id)
    try:
        record, placed = SEAT.seat_spawn.place_seat(slug, config, store, runtime, seat, now_ms, prepare)
    except SEAT.seat_spawn.SeatFailed as failed:
        if counted:
            expert_lineage.unlaunched(store, slug, lineage.id)
        store.drop_agent(slug, failed.record.name)
        if SEAT.seat_spawn.room_refused(failed.error):
            SEAT.transfers.detach(store, slug, failed.record)
            return [*left, WAITS.format(lineage=lineage.id, error=failed.error)]
        SEAT.transfers.failed(store, slug, failed.record)
        return [*left, FAILED.format(lineage=lineage.id, error=failed.error)]
    record = tick.placed_record(record, placed)
    store.put_agent(slug, record)
    SEAT.dispatch_seat.move_mail_home(store, record)
    return [*left, SPAWNED.format(name=record.name, lineage=lineage.id, number=number)]


def _left(slug: str, store: Any, lineage: expert_lineage.Lineage, number: int, now_ms: int) -> list[str]:
    """Leave the next expert the handoff its seat lacks when the last expert was lost: the stored one it never
    confirmed, offered again, or a recovery handoff that names what is unknown. Nothing is left for the first expert
    of a lineage or while a transfer still waits for its successor."""
    rows: list[dict[str, Any]] = SEAT.transfers.list_transfers(store, slug)
    if number == 1 or any(row["task"] == lineage.id and not row["successor"] for row in rows):
        return []
    name: str = store.seats.occupant(lineage.seat).occupant
    lost = AgentRecord(name, LANE, lineage.id, seat=lineage.seat)
    text: str = store.handoff(slug, lineage.id)
    if text:
        SEAT.transfers.record(store, slug, lost, REASON, text, now_ms)
        return [OFFERED.format(lineage=lineage.id, name=name)]
    keep_handoff(store, slug, lineage, lost, REASON, _recovery(slug, store, lineage, name), now_ms)
    return [RECOVERED.format(lineage=lineage.id, name=name)]


def _recovery(slug: str, store: Any, lineage: expert_lineage.Lineage, name: str) -> str:
    """The expert handoff the runtime writes for a seat whose expert was lost: the mission line, every deliverable
    pending and marked unknown, and the mission and the seat recap to read first."""
    mission = expert_lineage.mission(store, slug, lineage)
    count = len(expert_mission.deliverables(mission))
    pending = "\n".join(PENDING.format(number=number, name=name) for number in range(1, count + 1))
    read = [MISSION.format(address=lineage.mission)]
    if store.memory.recaps(lineage.seat):
        read.append(RECAP.format(seat=lineage.seat))
    return RECOVERY.format(line=expert_mission.line(slug, mission), name=name, pending=pending, read="\n".join(read))
