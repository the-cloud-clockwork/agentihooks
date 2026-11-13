"""The health findings block of swarm status text: each finding with its evidence, then the false positive counts."""

from typing import Any

from scripts.swarm.health import rows


def text(found: list[dict[str, Any]], false_positives: dict[str, dict[str, int]]) -> str:
    """The status lines for the visible findings, led by one false positives line when any finding is judged."""
    head = ""
    if false_positives:
        counts = (f"{kind} {row['false_positive']} of {row['judged']} judged" for kind, row in false_positives.items())
        head = "false positives  " + "  ".join(counts) + "\n"
    return head + "".join(rows.text(f) for f in found)
