"""Accept a quick seat's Handoff v2: check it, keep it in the seat folder and mark the seat for its successor."""

from collections.abc import Callable
from pathlib import Path

from scripts.handoff import check
from scripts.quick import paths, registry
from scripts.quick.member import Member

SEAT = "seat:"


def resolver(folder: Path) -> Callable[[str], bool]:
    """seat:<seat> resolves for a seat folder of this quick swarm; a GitHub link resolves through gh."""
    seats = folder / "seats"

    def resolves(address: str) -> bool:
        if address.startswith(SEAT):
            name = address.removeprefix(SEAT)
            return bool(name) and "/" not in name and ".." not in name and (seats / name).is_dir()
        if address.startswith("https://"):
            from scripts.handoff.resolve import Resolver

            return bool(Resolver("", None)(address))
        return False

    return resolves


def accept(found: Member, document: Path | None) -> list[str]:
    """Check the handoff, keep it as the seat's handoff and mark the seat pending; the problems when refused."""
    target = paths.handoff(found.folder, found.seat.seat)
    source = target if document is None else document
    try:
        text = source.read_text()
    except OSError:
        return [f"no handoff document at {source}"]
    problems = check.problems(text, resolver(found.folder))
    if problems:
        return list(problems)
    if source != target:
        target.write_text(text)
    registry.update(found.folder, found.seat.seat, state=registry.PENDING)
    return []
