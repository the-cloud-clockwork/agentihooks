"""The node cap gate: a write that raises an infrastructure node cap, or a command that resizes a node pool, waits
for the operator's authorization. It ships log only.
"""

import re
from pathlib import Path

from scripts.gates.base import Decision

NAME = "node-caps"
EDITS = frozenset({"Edit", "Write", "MultiEdit"})
POOL_MAXIMUM = re.compile(r"--nodes=\d+:(\d+):(\S+)")
NAMED_CAP = re.compile(
    r"\b(maxRunners|max_nodes|maxSize|max_size|max-size|max_node_count)\b[\"']?\s*[:=]\s*(\d+)\s*(?:,|#.*)?$", re.M
)
RESIZE = re.compile(
    r"instance-pool\s+update\b.*--size\b|\bnode-?(?:pool|group)s?\b.*(?:--max|--size|--nodes|scale\b|resize\b)"
    r"|\bscale\s+node-?group\b|\b(?:patch|set)\b.*maxRunners|--scaling-config\b.*maxSize=|\bresize\b.*--node-pool\b"
)
RESIZE_COMMAND = "a node pool resize command"


def refusal(found):
    return (
        f"node cap: {', '.join(found)} raises an infrastructure node cap, which only the operator authorizes. Raise "
        "the ask in Priorities through the master and continue other work."
    )


def caps(text):
    found = {}
    pairs = [(f"--nodes {pool}", top) for top, pool in POOL_MAXIMUM.findall(text)] + NAMED_CAP.findall(text)
    for key, value in pairs:
        found[key] = max(found.get(key, 0), int(value))
    return found


def raises(before, after):
    old = caps(before)
    return [f"{key} {old[key]} to {value}" for key, value in caps(after).items() if key in old and value > old[key]]


def resizes(command):
    return bool(RESIZE.search(command))


def _current(path):
    try:
        return Path(path).read_text()
    except (OSError, TypeError, ValueError):
        return None


def _edited(call):
    data = call.tool_input
    current = _current(data.get("file_path"))
    if call.tool == "Write":
        return (current, data["content"]) if current is not None else None
    edits = data["edits"] if call.tool == "MultiEdit" else [data]
    pairs = [(edit["old_string"], edit["new_string"]) for edit in edits if isinstance(edit, dict)]
    if current is None:
        return "\n".join(old for old, _ in pairs), "\n".join(new for _, new in pairs)
    after = current
    for old, new in pairs:
        after = after.replace(old, new, 1)
    return current, after


class NodeCaps:
    name = NAME
    default_mode = "observe"

    def matches(self, call):
        return call.tool in EDITS or call.tool == "Bash"

    def decide(self, call, who, state):
        if call.tool == "Bash":
            return Decision.deny(refusal([RESIZE_COMMAND])) if resizes(call.command) else Decision()
        edited = _edited(call)
        found = raises(*edited) if edited else []
        return Decision.deny(refusal(found)) if found else Decision()
