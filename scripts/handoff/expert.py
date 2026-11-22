"""The expert handoff: the stricter document an expert hands its lineage's seat off with.

It rides the Handoff v2 runtime unchanged: the command, the envelope, the transfer, the derived recap and the
successor's confirmation. Its body is wider. It names the saved mission, accounts for every deliverable of that
mission as done or pending, and carries the assumptions, the tests and the validation the expert made, so the next
expert continues exactly where this one stopped. The check is structural and never certifies meaning.
"""

import re
from collections.abc import Callable

from scripts.handoff import check
from scripts.swarm import expert_mission

HANDOFF = check.Contract(
    "# Expert handoff",
    (
        "Mission",
        "Done",
        "Pending",
        "Assumptions",
        "Tests",
        "Validation",
        "Stopped at",
        "Decisions and promises",
        "Next",
        "Read first",
    ),
    evidenced=("Done", "Tests", "Validation"),
)
ACCOUNTED = ("Done", "Pending")
DELIVERABLE = re.compile(r"D(\d+)\b")


def problems(text: str, resolves: Callable[[str], object], slug: str, mission: str) -> list[str]:
    """Every structural problem of an expert handoff for the lineage that works the given saved mission text. The
    mission line is the runtime's wording, so the wording rules read it as its address alone."""
    line = expert_mission.line(slug, mission)
    opened = check.section(text, "Mission").partition("\n")[0].strip()
    named = [number for heading in ACCOUNTED for number in _named(text, heading)]
    count = len(expert_mission.deliverables(mission))
    numbers = [str(number) for number in range(1, count + 1)]
    return [
        *check.problems(text.replace(line, expert_mission.address(slug, mission)), resolves, HANDOFF),
        *([] if opened == line else [f"## Mission must open with the mission line: {line}"]),
        *(_stray(written, count) for written in sorted(set(named) - set(numbers), key=lambda n: (len(n), n))),
        *(_accounted(number, named.count(number)) for number in numbers if named.count(number) != 1),
    ]


def _named(text: str, heading: str) -> list[str]:
    """The deliverable number each bullet of the section starts with, as written."""
    found = (DELIVERABLE.match(bullet) for bullet in check.bullets(check.section(text, heading)))
    return [match.group(1) for match in found if match]


def _stray(written: str, count: int) -> str:
    return (
        f"D{written} names no deliverable, the mission has {count}; number the bullet after the deliverable it "
        "accounts for or drop the number"
    )


def _accounted(number: str, times: int) -> str:
    if not times:
        return f"deliverable D{number} is named by no Done or Pending bullet; start one with D{number}"
    return f"deliverable D{number} is named by {times} bullets; name it once, under Done or under Pending"
