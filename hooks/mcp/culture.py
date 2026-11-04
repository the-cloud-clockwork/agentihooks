"""Culture MCP prompt and resources: the swarm culture and a seat's memory, reread on demand and fitted to the engine.

Under stdio an empty swarm, seat or engine falls back to the caller's environment
and registered seat; under a network transport the caller names them.
"""

import os
from typing import Any

from hooks.mcp._session import env_fallback_allowed

SERVER = "agentihooks"
CULTURE_URI = "swarm://{slug}/culture"
SEAT_URI = "swarm://{slug}/seats/{seat}"
REREAD = {
    "claude": "Reread them any time by mentioning @{server}:{culture} and @{server}:{seat}.",
    "codex": "Reread them any time with read_mcp_resource, server {server}, uri {culture} and uri {seat}.",
}
REREAD_ANY = "Reread them any time by reading the MCP resources {culture} and {seat} from server {server}."
NO_SWARM = "Name the swarm: this session belongs to no swarm, so there is no culture to read."
SHOWN = ("canon", "insight", "note")


def _store() -> Any:
    from scripts.swarm.store import connect

    return connect()


def _withheld(slug: str, address: str, culture: str, learned: list[dict[str, Any]]) -> dict[str, Any]:
    from scripts.swarm import priming_trace

    return priming_trace.withhold(slug, {"seat": address, "culture": culture, "learned": learned})


def culture_text(slug: str) -> str:
    """The swarm culture, with lines under a confirmed correction withheld."""
    culture = _withheld(slug, "", _store().culture.get(slug), [])["culture"]
    return culture if culture.strip() else f"Swarm {slug} has no culture yet."


def learned_lines(learned: list[dict[str, Any]]) -> list[str]:
    """Learned notes canon first, data and withheld or retired notes left out, data counted."""
    live = [note for note in learned if not note.get("withheld") and "retired" not in note]
    shown = sorted((note for note in live if note["maturity"] in SHOWN), key=lambda note: SHOWN.index(note["maturity"]))
    data = len(live) - len(shown)
    counted = [f"{data} data entries are kept on the seat and not shown."] if data else []
    if not shown:
        return ["Learned notes: none shown on this seat.", *counted]
    return ["Learned notes, canon first:", *(f"- {note['maturity']}: {note['text']}" for note in shown), *counted]


def seat_text(slug: str, seat: str) -> str:
    """The seat's latest recap and its learned notes."""
    address = f"{seat}@{slug}"
    store = _store()
    recaps = store.memory.recaps(address)
    learned = _withheld(slug, address, "", store.memory.learned(address))["learned"]
    if recaps:
        latest = recaps[0]
        lines = [f"Latest recap, by {latest['occupant']} on task {latest['task']}:", latest["text"]]
    else:
        lines = [f"Latest recap: no occupant of seat {seat} left one."]
    return "\n".join([f"Seat {address}", *lines, *learned_lines(learned)])


def _env(name: str) -> str:
    return os.environ.get(name, "").strip() if env_fallback_allowed() else ""


def _own_seat(slug: str) -> str:
    name = _env("AGENTIHOOKS_AGENT_NAME")
    agent = next((agent for agent in _store().agents(slug) if name and agent.name == name), None)
    return agent.seat.removesuffix(f"@{slug}") if agent and agent.seat else ""


def _engine(engine: str) -> str:
    from hooks.targets import current_target

    return engine.strip().lower() or (current_target() if env_fallback_allowed() else "")


def swarm_culture(engine: str = "", swarm: str = "", seat: str = "") -> str:
    """The swarm culture and the seat's memory as one message, closing with how this engine rereads them."""
    slug = swarm.strip() or _env("AGENTIHOOKS_SWARM")
    if not slug:
        return NO_SWARM
    seat = seat.strip() or _own_seat(slug)
    parts = [f"Swarm culture of {slug}:", culture_text(slug)]
    if seat:
        parts += ["", seat_text(slug, seat)]
    uris = {
        "server": SERVER,
        "culture": CULTURE_URI.format(slug=slug),
        "seat": SEAT_URI.format(slug=slug, seat=seat or "<seat>"),
    }
    parts += ["", REREAD.get(_engine(engine), REREAD_ANY).format(**uris)]
    return "\n".join(parts)


def register(mcp: Any) -> None:
    """Register the culture resources and prompt on a FastMCP server."""
    mcp.resource(
        CULTURE_URI,
        name="swarm_culture",
        description="The culture every seat of the swarm shares.",
        mime_type="text/markdown",
    )(culture_text)
    mcp.resource(
        SEAT_URI,
        name="seat_memory",
        description="A seat's latest recap and learned notes, canon first; seat is its label such as eng-3.",
        mime_type="text/markdown",
    )(seat_text)
    mcp.prompt(
        name="swarm_culture",
        description="Reload the swarm culture and your seat's memory. engine is claude or codex; swarm and seat "
        "default to your own under stdio.",
    )(swarm_culture)
