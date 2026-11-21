"""The recon gate: a swarm engineer or CI agent writes recon.md in its task work folder before its first code edit.

recon.md holds the six HEADINGS in order, each followed by at least one non blank line before the next level one or two
heading, in at most MAX_LINES lines. The gate judges the build gate's calls on the build gate's paths, so an edit inside
the task work folder, ~/scratchpad or outside any git work tree passes and the agent can write the file. A pass is
counted in the gate log once per recon.md text of a task.
"""

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.gates import log
from scripts.gates.base import Call, Decision, Who
from scripts.gates.build import LANES, BuildGate
from scripts.swarm import naming
from scripts.swarm_ledger import ledger_workspace

if TYPE_CHECKING:
    from scripts.gates.verdicts import Verdicts

RECON = "recon.md"
MAX_LINES = 500
HEADINGS = (
    "## What will be built",
    "## Code areas touched",
    "## What might break",
    "## Regression tests",
    "## Resources needed",
    "## Protection plan",
)
NAMING = (HEADINGS[1], HEADINGS[3])
SECTION = re.compile(r"^#{1,2}\s")


def sections(text: str) -> list[tuple[str, list[str]]]:
    """Each level one or two heading of text, stripped, with its lines up to the next such heading."""
    found: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if SECTION.match(line):
            found.append((line.strip(), []))
        elif found:
            found[-1][1].append(line)
    return found


def problem(text: str) -> str:
    """What keeps text from being a valid recon plan, or an empty string when it is one."""
    count = len(text.splitlines())
    if count > MAX_LINES:
        return f"it holds {count} lines, more than {MAX_LINES}"
    held = [(heading, body) for heading, body in sections(text) if heading in HEADINGS]
    order = [heading for heading, _ in held]
    missing = [heading for heading in HEADINGS if heading not in order]
    if missing:
        return f'the heading "{missing[0]}" is missing'
    if order != list(HEADINGS):
        return f"the headings are out of order or repeated; write each once, in this order: {', '.join(HEADINGS)}"
    empty = [heading for heading, body in held if not any(line.strip() for line in body)]
    return f'the heading "{empty[0]}" has no content' if empty else ""


def valid(folder: Path) -> bool:
    """True when folder holds a valid recon plan."""
    try:
        return not problem((folder / RECON).read_text())
    except OSError:
        return False


def named(folder: Path) -> str:
    """The lines under the recon.md headings that name code areas and regression tests, or an empty string."""
    try:
        text = (folder / RECON).read_text()
    except OSError:
        return ""
    return "\n".join(line for heading, body in sections(text) if heading in NAMING for line in body)


def refusal(path: Path, found: str) -> str:
    return (
        f"recon gate: {path}: {found}. Before the first code edit write it with these headings in this order, each "
        f"followed by its content, at most {MAX_LINES} lines: {', '.join(HEADINGS)}"
    )


class ReconGate:
    name = "recon"
    default_mode = "observe"

    def __init__(self) -> None:
        self.build = BuildGate()

    def matches(self, call: Call) -> bool:
        return self.build.matches(call)  # pyright: ignore[reportUnknownMemberType]

    def decide(self, call: Call, who: Who, state: "Verdicts") -> Decision:
        if not (who.pinned and who.task) or naming.lane_of(who.name) not in LANES:
            return Decision()
        folder = ledger_workspace.folder(who.swarm, who.task)
        if not self.build.touched(call, folder):
            return Decision()
        path = folder / RECON
        try:
            text = path.read_text()
        except OSError:
            return Decision(allowed=False, reason=refusal(path, "no recon plan yet"))
        found = problem(text)
        if found:
            return Decision(allowed=False, reason=refusal(path, found))
        self.count(state, who, call.tool, path, text)
        return Decision()

    def count(self, state: "Verdicts", who: Who, tool: str, path: Path, text: str) -> None:
        """Log one count row the first time this recon.md text passes for the task."""
        digest = hashlib.sha256(text.encode()).hexdigest()
        held = state.read(who.task) or {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if held.get("digest") == digest:  # pyright: ignore[reportUnknownMemberType]
            return
        reason = f"valid recon plan: {path}"
        state.write(who.task, "pass", reason, digest=digest)  # pyright: ignore[reportUnknownMemberType]
        row = log.Row.of(self.name, "count", who, tool, reason)  # pyright: ignore[reportUnknownMemberType]
        log.append(state.slug, row, state.home)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
