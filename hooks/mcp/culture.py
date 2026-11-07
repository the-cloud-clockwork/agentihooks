"""Culture MCP category: the swarm culture and a seat's memory as resources, and a priming prompt fitted to the engine."""

import os
import re
from importlib import import_module
from typing import Any

SERVER = "agentihooks"
CULTURE_URI = "swarm://{slug}/culture"
SEAT_URI = "swarm://{slug}/seats/{seat}"


def _module(name: str) -> Any:
    """A swarm module, imported on first use so listing never loads it."""
    return import_module(name)


def _store() -> Any:
    return _module("scripts.swarm.store").connect()


def _withhold(slug: str, task: dict[str, Any]) -> dict[str, Any]:
    return _module("scripts.swarm.priming_trace").withhold(slug, task)


def _address(slug: str, seat: str) -> str:
    """The full seat address for a seat label or address, refused when it belongs to another swarm."""
    address = seat if "@" in seat else f"{seat}@{slug}"
    if not address.endswith(f"@{slug}"):
        raise ValueError(f"{seat} is not a seat of swarm {slug}")
    return address


def culture_text(store: Any, slug: str) -> str:
    """The swarm culture with every line under a confirmed correction withheld, as the opening prompt carries it."""
    text = store.culture.get(slug) or ""
    return _withhold(slug, {"seat": "", "culture": text, "learned": []})["culture"]


def seat_text(store: Any, slug: str, seat: str) -> str:
    """The seat's latest recap and its learned notes, canon first, data counted and not shown."""
    address = _address(slug, seat)
    recaps = store.memory.recaps(address)
    learned = _withhold(slug, {"seat": address, "culture": "", "learned": store.memory.learned(address)})["learned"]
    lines = [f"Seat {address}."]
    if recaps:
        lines += [f"Latest recap, by {recaps[0]['occupant']} on task {recaps[0]['task']}:", recaps[0]["text"]]
    else:
        lines.append("Latest recap: none, no occupant of this seat left one.")
    notes: list[str] = _module("scripts.swarm.prompt").learned_lines(learned)
    return "\n".join([*lines, re.sub(r"^\d+\. ", "", notes[0]), *notes[1:]]) + "\n"


def reread_line(engine: str, slug: str, label: str) -> str:
    """How an agent on this engine rereads the culture and seat resources after compaction."""
    culture = CULTURE_URI.format(slug=slug)
    seat = SEAT_URI.format(slug=slug, seat=label)
    if engine == "codex":
        return f"Reread them after compaction with read_mcp_resource on server {SERVER}, uri {culture} and uri {seat}."
    if engine == "claude":
        return f"Reread them after compaction by mentioning @{SERVER}:{culture} and @{SERVER}:{seat}."
    return f"Reread them after compaction from the {SERVER} MCP resources {culture} and {seat}."


def _caller_seat(store: Any, slug: str) -> str:
    name = store.names.resolve(_module("scripts.gates").Who.from_env().name)
    agent = next((agent for agent in store.agents(slug) if agent.name == name), None)
    return agent.seat if agent is not None else ""


def resolve(store: Any, engine: str, swarm: str, seat: str) -> tuple[str, str, str]:
    """Engine, swarm and seat for the prompt; under stdio an empty one falls back to this session, else it is required."""
    from hooks.mcp._session import env_fallback_allowed
    from hooks.targets import current_target

    if env_fallback_allowed():
        engine = engine or current_target()
        swarm = swarm or os.getenv("AGENTIHOOKS_SWARM", "")
        seat = seat or (_caller_seat(store, swarm) if swarm else "")
    missing = [name for name, value in (("engine", engine), ("swarm", swarm), ("seat", seat)) if not value]
    if missing:
        raise ValueError(f"swarm_culture needs {', '.join(missing)}")
    return engine.lower(), swarm, _address(swarm, seat)


def priming(store: Any, engine: str, swarm: str, seat: str) -> str:
    """The culture and seat memory as one message, closing with how this engine rereads them."""
    engine, swarm, address = resolve(store, engine, swarm, seat)
    culture = culture_text(store, swarm).strip() or "none written for this swarm yet."
    return "\n".join(
        [
            f"Swarm culture of {swarm}, shared by every seat:",
            culture,
            "",
            seat_text(store, swarm, address).rstrip("\n"),
            "",
            reread_line(engine, swarm, address.rsplit("@", 1)[0]),
        ]
    )


def register(mcp: Any) -> None:
    """Register the culture and seat resource templates and the swarm_culture prompt."""
    mcp.resource(CULTURE_URI, name="swarm_culture", mime_type="text/markdown")(swarm_culture_resource)
    mcp.resource(SEAT_URI, name="swarm_seat", mime_type="text/markdown")(swarm_seat_resource)
    mcp.prompt(name="swarm_culture")(swarm_culture)


def swarm_culture_resource(slug: str) -> str:
    """The swarm culture shared by every seat, with lines under a confirmed correction withheld."""
    return culture_text(_store(), slug)


def swarm_seat_resource(slug: str, seat: str) -> str:
    """A seat's latest recap and learned notes, canon first, data counted and not shown."""
    return seat_text(_store(), slug, seat)


def swarm_culture(engine: str = "", swarm: str = "", seat: str = "") -> str:
    """The swarm culture and this seat's memory, closing with how this engine rereads them.

    Args:
        engine: claude or codex; under stdio empty means AGENTIHOOKS_TARGET.
        swarm: the swarm slug; under stdio empty means AGENTIHOOKS_SWARM.
        seat: a seat label or address; under stdio empty means the caller's registered seat.
    """
    return priming(_store(), engine, swarm, seat)
