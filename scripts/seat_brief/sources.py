"""Where a seat's brief is read from: the swarm Redis seat store or a quick swarm's seat files."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.gates.base import Who
from scripts.handoff import check, transfers
from scripts.inbox.seats import MATURITIES
from scripts.quick import member, paths
from scripts.swarm import naming, priming_trace
from scripts.swarm import store as swarm_store

RECAP_SECTIONS = ("Done", "Stopped at", "Next")
DATA = "data"


@dataclass(frozen=True)
class Brief:
    """One seat's context in reading order: handoff, culture, latest recap and learned notes."""

    seat: str
    handoff: str
    culture: str
    recap: str
    learned: str


class SeatSource(Protocol):
    """A store that holds one seat's context."""

    def brief(self) -> Brief | None: ...


def recap_of(handoff: str) -> str:
    """The recap a Handoff v2 document yields, its Done, Stopped at and Next sections, as the swarm handoff derives it."""
    found = [(heading, check.section(handoff, heading)) for heading in RECAP_SECTIONS]
    return "\n\n".join(f"## {heading}\n{text}" for heading, text in found if text)


def read_file(path: Path) -> str:
    """A quick seat file's text, empty when the file does not exist; any other read failure raises its OSError."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def learned_text(notes: list[dict[str, Any]]) -> str:
    """The seat's learned notes canon first, as the priming shows them: data entries counted, withheld notes left out."""
    rank = {maturity: -level for level, maturity in enumerate(MATURITIES)}
    kept = [note for note in notes if not note.get("withheld")]
    shown = sorted((note for note in kept if note["maturity"] != DATA), key=lambda note: rank[note["maturity"]])
    lines = [f"- {note['maturity']}: {note['text']}" for note in shown]
    data = len(kept) - len(shown)
    return "\n".join([*lines, f"{data} data entries are kept on the seat and not shown."] if data else lines)


@dataclass(frozen=True)
class RedisSeat:
    """A swarm seat, read from the swarm Redis store through the agent record of the session's name."""

    slug: str
    name: str
    store: Callable[[], Any]

    def brief(self) -> Brief | None:
        store: Any = self.store()
        name = store.names.resolve(self.name)
        agent: Any = next((found for found in store.agents(self.slug) if found.name == name), None)
        if agent is None or not agent.seat:
            return None
        recaps: list[dict[str, Any]] = store.memory.recaps(agent.seat)
        held = priming_trace.withhold(
            self.slug,
            {
                "seat": agent.seat,
                "culture": store.culture.get(self.slug),
                "learned": store.memory.scoped(self.slug, agent.seat),
            },
        )
        return Brief(
            seat=agent.seat,
            handoff=store.handoff(self.slug, agent.task) or transfers.last_handoff(store, self.slug, agent.task),  # pyright: ignore[reportUnknownMemberType]
            culture=held["culture"],
            recap=recaps[0]["text"] if recaps else "",
            learned=learned_text(held["learned"]),
        )


@dataclass(frozen=True)
class QuickSeat:
    """A quick swarm seat, read from its seat folder and the swarm's culture file."""

    found: member.Member

    def brief(self) -> Brief:
        folder, seat = self.found.folder, self.found.seat.seat
        handoff = read_file(paths.handoff(folder, seat))
        return Brief(
            seat=f"{seat} of quick swarm {self.found.swarm.quick_id}",
            handoff=handoff,
            culture=read_file(paths.culture(folder)),
            recap=recap_of(handoff),
            learned=read_file(paths.learned(folder, seat)),
        )


def connect() -> Any:
    """The swarm Redis store the session's environment names."""
    return swarm_store.connect()  # pyright: ignore[reportUnknownMemberType]


def find(environ: Mapping[str, str]) -> SeatSource | None:
    """The store of the seat this session holds: its quick seat, else its swarm seat; None for a standalone session."""
    quick = member.find(environ)
    if quick is not None:
        return QuickSeat(quick)
    who = Who.from_env(environ)  # pyright: ignore[reportUnknownMemberType]
    if who.pinned and naming.lane_of(who.name):
        return RedisSeat(who.swarm, who.name, connect)
    return None
