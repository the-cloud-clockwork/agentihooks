"""An expert mission: the one document a lineage of experts works to its end.

A mission is written from one template: the operator's words, the problem, the evidence, what to deliver as a
numbered list and how to report. This module holds that template, its structural check, the numbered deliverables
and the address every expert of a lineage reads the same saved text by. The check is structural: it never judges
whether a mission is a good one.
"""

import hashlib
import re

from scripts.handoff import check

TITLE = "# Mission: "
DELIVER = "What to deliver"
HEADINGS = ("The operator's words", "The problem", "Evidence", DELIVER, "How to report")
SCHEME = "mission"
NUMBERED = re.compile(r"(\d+)[.)]\s+(\S.*)")
TEMPLATE = f"""{TITLE}<one line naming the outcome>

## {HEADINGS[0]}
<his words verbatim, with the date and where he typed them; None when no operator order stands behind the mission>

## {HEADINGS[1]}
<what is wrong or missing, in plain words>

## {HEADINGS[2]}
<each observed fact and how it was observed; mark what is unproven so the expert verifies it before building on it>

## {HEADINGS[3]}
1. <the first deliverable and the check that proves it>
2. <the second deliverable and the check that proves it>

## {HEADINGS[4]}
<who is told what and when, with the command; who answers what the mission leaves open>
"""


def title(text: str) -> str:
    """The mission's one line title, empty when its first line is no mission title."""
    first = text.strip().partition("\n")[0].strip()
    return first[len(TITLE) :].strip() if first.startswith(TITLE) else ""


def deliverables(text: str) -> list[str]:
    """The numbered deliverables in the order written, each one line; empty unless they count from 1 with no gap."""
    found = [match for line in check.section(text, DELIVER).splitlines() if (match := NUMBERED.fullmatch(line))]
    counted = [int(match.group(1)) for match in found] == list(range(1, len(found) + 1))
    return [match.group(2).strip() for match in found] if counted else []


def problems(text: str) -> list[str]:
    """Every structural problem of a mission document, empty when it conforms."""
    names = check.headings(text)
    found = [] if title(text) else [f"the first line must be {TITLE}followed by the mission's title"]
    for heading in HEADINGS:
        if names.count(heading) != 1:
            found.append(f"## {heading} must appear once and appears {names.count(heading)} times")
        elif not check.section(text, heading):
            found.append(f"## {heading} is empty")
    known = list(dict.fromkeys(name for name in names if name in HEADINGS))
    if known != [heading for heading in HEADINGS if heading in known]:
        found.append(f"the headings are out of order; keep {', '.join(HEADINGS)}")
    if check.section(text, DELIVER) and not deliverables(text):
        found.append(f"## {DELIVER} numbers its deliverables from 1 with no gap, one per line")
    return [*found, *check.leaks(text)]


def digest(text: str) -> str:
    """The sha256 of the mission text, the key its saved copy is stored and addressed by."""
    return hashlib.sha256(text.encode()).hexdigest()


def address(slug: str, text: str) -> str:
    """The address of this mission text in the swarm: the same for every lineage that starts from the same text."""
    return f"{SCHEME}:{slug}/{digest(text)}"


def line(slug: str, text: str) -> str:
    """The one line that names a mission wherever it is cited: its address, then its title."""
    return f"{address(slug, text)} {title(text)}"
