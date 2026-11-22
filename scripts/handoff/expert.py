"""The two documents of an expert: the expert handoff it hands its lineage's seat off with, and the mission proof its
lineage ends on.

The handoff rides the Handoff v2 runtime unchanged: the command, the envelope, the transfer, the derived recap and the
successor's confirmation. Its body is wider. It names the saved mission, accounts for every deliverable of that
mission as done or pending, and carries the assumptions, the tests and the validation the expert made, so the next
expert continues exactly where this one stopped. The proof names every deliverable as proven, each kind of check that
holds the work, and nothing left open. Both checks are structural and never certify meaning.
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
PROOF = check.Contract(
    "# Mission proof",
    (
        "Mission",
        "Deliverables",
        "Validation",
        "Smoke tests",
        "Regression tests",
        "Semgrep rules",
        "Feature map",
        "Open items",
    ),
    evidenced=("Deliverables", "Validation", "Smoke tests", "Regression tests", "Semgrep rules", "Feature map"),
    marker="<!-- mission complete -->",
    shown=True,
)
ACCOUNTED = ("Done", "Pending")
CHECKED = PROOF.evidenced[1:]
DELIVERABLE = re.compile(r"D(\d+)\b")
PULL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def problems(text: str, resolves: Callable[[str], object], slug: str, mission: str) -> list[str]:
    """Every structural problem of an expert handoff for the lineage that works the given saved mission text. The
    mission line is the runtime's wording, so the wording rules read it as its address alone."""
    line = expert_mission.line(slug, mission)
    named = [number for heading in ACCOUNTED for number in _numbers(check.section(text, heading))]
    count = len(expert_mission.deliverables(mission))
    return [
        *check.problems(text.replace(line, expert_mission.address(slug, mission)), resolves, HANDOFF),
        *_opening(text, line),
        *_strangers(named, count),
        *(_accounted(n, named.count(n)) for n in range(1, count + 1) if named.count(n) != 1),
    ]


def proof_problems(
    text: str, resolves: Callable[[str], object], merged: Callable[[str], object], slug: str, mission: str
) -> list[str]:
    """Every structural problem of a mission proof for the lineage that works the given saved mission text: each
    deliverable proven by one bullet, each kind of check named with its evidence, nothing left open and every pull
    request under Deliverables merged. merged(url) says a pull request is merged."""
    line = expert_mission.line(slug, mission)
    proven = check.section(text, "Deliverables")
    named = _numbers(proven)
    count = len(expert_mission.deliverables(mission))
    return [
        *check.problems(text.replace(line, expert_mission.address(slug, mission)), resolves, PROOF),
        *_opening(text, line),
        *(
            f"Deliverables bullet {number} names no deliverable; start it with D and its number"
            for number, bullet in enumerate(check.bullets(proven), 1)
            if not DELIVERABLE.match(bullet)
        ),
        *_strangers(named, count),
        *(_proven(n, named.count(n)) for n in range(1, count + 1) if named.count(n) != 1),
        *(
            f"## {heading} holds no bullet; name at least one check with its evidence"
            for heading in CHECKED
            if not check.bullets(check.section(text, heading))
        ),
        *_left(check.section(text, "Open items")),
        *(f"pull request {url} is not merged" for url in dict.fromkeys(PULL.findall(proven)) if not merged(url)),
    ]


def _opening(text: str, line: str) -> list[str]:
    opened = check.section(text, "Mission").partition("\n")[0].strip()
    return [] if opened == line else [f"## Mission must open with the mission line: {line}"]


def _numbers(body: str) -> list[int]:
    found = (DELIVERABLE.match(bullet) for bullet in check.bullets(body))
    return [int(match.group(1)) for match in found if match]


def _strangers(named: list[int], count: int) -> list[str]:
    return [f"D{n} names no deliverable; the mission has {count}" for n in sorted(set(named)) if not 1 <= n <= count]


def _accounted(number: int, times: int) -> str:
    if not times:
        return f"deliverable D{number} is named by no Done or Pending bullet; start one with D{number}"
    return f"deliverable D{number} is named by {times} bullets; name it once, under Done or under Pending"


def _proven(number: int, times: int) -> str:
    if not times:
        return f"deliverable D{number} is named by no Deliverables bullet; start one with D{number}"
    return f"deliverable D{number} is named by {times} bullets; name it once under Deliverables"


def _left(open_items: str) -> list[str]:
    if open_items in ("", check.NONE):
        return []
    return ["## Open items must be None; a lineage ends with nothing open"]
