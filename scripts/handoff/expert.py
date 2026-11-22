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
REFERENCE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+"
    r"|(?:(?<![\w.-])[\w.-]{1,39}/[\w.-]{1,100})?#\d+\b",
    re.IGNORECASE,
)
UNREAD = (
    "{written} under Deliverables cannot be read as a merged pull request; write a pull request as its full link "
    "https://github.com/<owner>/<repo>/pull/<number> and anything that is no pull request in words"
)
ACCOUNTS = "number the bullet after the deliverable it accounts for or drop the number"
PROVES = "number the bullet after the deliverable it proves"


def problems(text: str, resolves: Callable[[str], object], slug: str, mission: str) -> list[str]:
    """Every structural problem of an expert handoff for the lineage that works the given saved mission text. The
    mission line is the runtime's wording, so the wording rules read it as its address alone."""
    line = expert_mission.line(slug, mission)
    named = [number for heading in ACCOUNTED for number in _numbers(check.section(text, heading))]
    count = len(expert_mission.deliverables(mission))
    return [
        *check.problems(text.replace(line, expert_mission.address(slug, mission)), resolves, HANDOFF),
        *_opening(text, line),
        *_strangers(named, count, ACCOUNTS),
        *(_accounted(number, named.count(number)) for number in _expected(count) if named.count(number) != 1),
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
        *_strangers(named, count, PROVES),
        *(_proven(number, named.count(number)) for number in _expected(count) if named.count(number) != 1),
        *(
            f"## {heading} holds no bullet; name at least one check with its evidence"
            for heading in CHECKED
            if not check.bullets(check.section(text, heading))
        ),
        *_left(check.section(text, "Open items")),
        *(
            UNREAD.format(written=written)
            for written in dict.fromkeys(REFERENCE.findall(proven))
            if not PULL.fullmatch(written)
        ),
        *(f"pull request {url} is not merged" for url in dict.fromkeys(PULL.findall(proven)) if not merged(url)),
    ]


def _opening(text: str, line: str) -> list[str]:
    opened = check.section(text, "Mission").partition("\n")[0].strip()
    return [] if opened == line else [f"## Mission must open with the mission line: {line}"]


def _numbers(body: str) -> list[str]:
    """The deliverable number each bullet of a section body starts with, as written."""
    found = (DELIVERABLE.match(bullet) for bullet in check.bullets(body))
    return [match.group(1) for match in found if match]


def _expected(count: int) -> list[str]:
    """The numbers a mission of that many deliverables names them by."""
    return [str(number) for number in range(1, count + 1)]


def _strangers(named: list[str], count: int, fix: str) -> list[str]:
    return [
        f"D{written} names no deliverable, the mission has {count}; {fix}"
        for written in sorted(set(named) - set(_expected(count)), key=lambda n: (len(n), n))
    ]


def _accounted(number: str, times: int) -> str:
    if not times:
        return f"deliverable D{number} is named by no Done or Pending bullet; start one with D{number}"
    return f"deliverable D{number} is named by {times} bullets; name it once, under Done or under Pending"


def _proven(number: str, times: int) -> str:
    if not times:
        return f"deliverable D{number} is named by no Deliverables bullet; start one with D{number}"
    return f"deliverable D{number} is named by {times} bullets; name it once under Deliverables"


def _left(open_items: str) -> list[str]:
    if open_items in ("", check.NONE):
        return []
    return ["## Open items must be None; a lineage ends with nothing open"]
