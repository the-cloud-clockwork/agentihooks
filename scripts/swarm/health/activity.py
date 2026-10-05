"""Watch and action counts per swarm agent, recorded at PreToolUse and read by the health findings."""

import json
import os
import re
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
WATCH_TOOLS = ("Monitor", "TaskOutput", "BashOutput")
ACT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SERENA_EDITS = (
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "rename_symbol",
    "replace_content",
    "replace_in_files",
    "safe_delete_symbol",
)
WATCH_RE = re.compile(r"\b(ledger\s+watch|gh\s+pr\s+checks|gh\s+run\s+(watch|view)|swarm\s+\S+\s+status|sleep\s)")
ACT_RE = re.compile(r"\b(git\s+commit|git\s+push|gh\s+pr\s+(create|merge))\b")


def default_root():
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "swarm-activity"


def classify(tool_name, tool_input):
    serena_edit = tool_name.startswith("mcp__serena__") and tool_name.rsplit("__", 1)[-1] in SERENA_EDITS
    if tool_name in ACT_TOOLS or serena_edit:
        return "act"
    if tool_name in WATCH_TOOLS:
        return "watch"
    command = (tool_input or {}).get("command", "") if tool_name == "Bash" else ""
    if ACT_RE.search(command):
        return "act"
    return "watch" if WATCH_RE.search(command) else ""


def record(tool_name, tool_input, environ=None, root=None):
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM", ""), env.get("AGENTIHOOKS_AGENT_NAME", "")
    kind = classify(tool_name, tool_input)
    if not (kind and NAME_RE.match(slug) and NAME_RE.match(name)):
        return
    folder = Path(root or default_root()) / slug
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{name}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind}) + "\n")


def counts(slug, root=None):
    folder = Path(root or default_root()) / slug
    found = {}
    for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
        tally = {"watch": 0, "act": 0}
        for line in path.read_text(encoding="utf-8").splitlines():
            kind = json.loads(line).get("kind") if line.strip() else None
            if kind in tally:
                tally[kind] += 1
        found[path.stem] = tally
    return found
