"""The health findings block of swarm status text: each finding with its evidence, then the false positive counts."""

from typing import Any


def text(found: list[dict[str, Any]], false_positives: dict[str, dict[str, int]]) -> str:
    """The status lines for the visible findings, led by one false positives line when any finding is judged."""
    lines = []
    if false_positives:
        counts = (f"{kind} {row['false_positive']} of {row['judged']} judged" for kind, row in false_positives.items())
        lines.append("false positives  " + "  ".join(counts))
    for f in found:
        lines.append(f"finding  {f['kind']}  {f['subject']}: {f['summary']}")
        lines.extend(f"  - {entry}" for entry in f["evidence"])
        lines.append(f"  threshold {f['threshold']}")
        lines.append(f"  id {f['id']}" + (f"  earlier verdict {f['verdict']['value']}" if f["verdict"] else ""))
    return "".join(f"{line}\n" for line in lines)
