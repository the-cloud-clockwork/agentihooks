"""Watch and action counts per swarm agent, recorded at PreToolUse and read by the health findings."""

import json
import os
import re
import shutil
import time
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][\w.@-]{0,63}$")
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
WATCH_RE = re.compile(r"\b(ledger\s+watch|gh\s+pr\s+checks|gh\s+run\s+(watch|view)|sleep\s)")
SWARM_CMD = r"\bagentihooks\s+swarm\s+(?:--as\s+\S+\s+)?[a-z][\w-]*\s+(?:--as\s+\S+\s+)?"
ACT_RE = re.compile(
    r"\b(git\s+commit|git\s+push|gh\s+pr\s+(create|merge))\b"
    r"|\bagentihooks\s+ledger\s+(?:--\S+\s+\S+\s+)*(comment|say|phase|task|followup)\b"
    rf"|{SWARM_CMD}(say|learned|done|pr)\b"
    r"|\bagentihooks\s+msg\s+reply\b"
)
NEITHER_RE = re.compile(rf"{SWARM_CMD}(status|verdict)\b")
REARM_RE = re.compile(r"\bledger\s+watch\b")
REARM_WINDOW_MS = 30 * 60_000
REVIVE_MARK_MS = 60_000


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
    if NEITHER_RE.search(command):
        return ""
    return "watch" if WATCH_RE.search(command) else ""


def record(tool_name, tool_input, environ=None, root=None, now_ms=None):
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM", ""), env.get("AGENTIHOOKS_AGENT_NAME", "")
    if not (NAME_RE.match(slug) and NAME_RE.match(name)):
        return
    at = int(time.time() * 1000) if now_ms is None else now_ms
    folder = Path(root or default_root()) / slug
    first = folder / f"{name}.first"
    if not first.exists():
        folder.mkdir(parents=True, exist_ok=True)
        first.write_text(str(at), encoding="utf-8")
    (folder / f"{name}.last").write_text(str(at), encoding="utf-8")
    kind = classify(tool_name, tool_input)
    if not kind:
        return
    entry = {"kind": kind, "at": at}
    if kind == "watch" and REARM_RE.search((tool_input or {}).get("command", "")):
        entry["rearm"] = True
        if _take_mark(folder / f"{name}.revive", at):
            entry["revived"] = True
    with open(folder / f"{name}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def mark_revived(slug, name, root=None, now_ms=None):
    if not (NAME_RE.match(slug) and NAME_RE.match(name)):
        return
    folder = Path(root or default_root()) / slug
    folder.mkdir(parents=True, exist_ok=True)
    at = int(time.time() * 1000) if now_ms is None else now_ms
    (folder / f"{name}.revive").write_text(str(at))


def _take_mark(path, at):
    try:
        marked = int(path.read_text())
        path.unlink()
    except (OSError, ValueError):
        return False
    return at - marked <= REVIVE_MARK_MS


def _rows(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def entries(slug, root=None):
    folder = Path(root or default_root()) / slug
    return {path.stem: _rows(path) for path in (sorted(folder.glob("*.jsonl")) if folder.is_dir() else [])}


def rows_of(slug, name, root=None):
    path = Path(root or default_root()) / slug / f"{name}.jsonl"
    return _rows(path) if path.is_file() else []


def since_action(rows):
    last = max((index for index, entry in enumerate(rows) if entry.get("kind") == "act"), default=-1)
    return tally(rows[last + 1 :])["watch"]


def tally(rows):
    found, armed = {"watch": 0, "act": 0}, None
    for entry in rows:
        if entry.get("revived"):
            continue
        if entry.get("rearm"):
            if armed is not None and entry["at"] - armed < REARM_WINDOW_MS:
                continue
            armed = entry["at"]
        if entry.get("kind") in found:
            found[entry["kind"]] += 1
    return found


def counts(slug, root=None):
    return {name: {**tally(rows), "since": since_action(rows)} for name, rows in entries(slug, root).items()}


def first_events(slug, root=None):
    folder = Path(root or default_root()) / slug
    found = {}
    for path in sorted(folder.glob("*.first")) if folder.is_dir() else []:
        try:
            found[path.stem] = int(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
    return found


def last_events(slug: str, root=None) -> dict[str, int]:
    folder = Path(root or default_root()) / slug
    found = {}
    for path in sorted(folder.glob("*.last")) if folder.is_dir() else []:
        try:
            found[path.stem] = int(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
    return found


def clear(slug, root=None):
    base = Path(root or default_root()).resolve()
    folder = (base / slug).resolve()
    if folder.parent != base:
        raise ValueError(f"refusing to clear swarm activity for {slug!r}: not one folder under {base}")
    shutil.rmtree(folder, ignore_errors=True)
