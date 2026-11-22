"""An expert mission: the one document a lineage of experts works to its end.

A mission is written from one template: the operator's words, the problem, the evidence, what to deliver as a
numbered list and how to report. This module holds that template, its structural check, the numbered deliverables
and the address every expert of a lineage reads the same saved text by. The check is structural: it never judges
whether a mission is a good one.
"""

TITLE = "# Mission: "
DELIVER = "What to deliver"
HEADINGS = ("The operator's words", "The problem", "Evidence", DELIVER, "How to report")
SCHEME = "mission"
TEMPLATE = ""


def title(text: str) -> str:
    """The mission's one line title, empty when its first line is no mission title."""
    return ""


def deliverables(text: str) -> list[str]:
    """The numbered deliverables in the order written, each one line; empty unless they count from 1 with no gap."""
    return []


def problems(text: str) -> list[str]:
    """Every structural problem of a mission document, empty when it conforms."""
    return []


def digest(text: str) -> str:
    """The sha256 of the mission text, the key its saved copy is stored and addressed by."""
    return ""


def address(slug: str, text: str) -> str:
    """The address of this mission text in the swarm: the same for every lineage that starts from the same text."""
    return ""


def line(slug: str, text: str) -> str:
    """The one line that names a mission wherever it is cited: its address, then its title."""
    return ""
