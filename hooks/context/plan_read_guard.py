import json
import os
import re
from pathlib import Path, PurePath

LANES = {"eng", "ci"}
MARGIN = 10
PLAN_ID = re.compile(r"[0-9a-f]{64}\.md")
SED_RANGE = re.compile(r"\s*sed\s+-n\s+(['\"]?)([1-9][0-9]*),([1-9][0-9]*)p\1\s+(\S+)\s*")
FIELDS = {"Read": ("file_path",), "Grep": ("path", "glob"), "Bash": ("command",), "WebFetch": ("url",)}
COMMAND = "agentihooks plan read"


def enabled(env) -> bool:
    return env.get("PLAN_READ_GUARD_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def check(payload: dict, environ=None) -> str | None:
    env = os.environ if environ is None else environ
    slug = env.get("AGENTIHOOKS_SWARM", "")
    if not (enabled(env) and slug and env.get("AGENTIHOOKS_SWARM_LANE") in LANES):
        return None
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if tool_name not in FIELDS or not isinstance(tool_input, dict):
        return None
    text = " ".join(str(tool_input.get(field) or "") for field in FIELDS[tool_name])
    folder = re.compile(re.escape(f"{slug}.media") + r"(?!/[0-9a-f]{64}\.)")
    if not PLAN_ID.search(text) and not folder.search(text):
        return None
    plans, window = _plans(env, slug)
    named = set(PLAN_ID.findall(text))
    if not (named & plans or (plans and folder.search(text))):
        return None
    if window and named == {window[0]} and DISPATCH[tool_name](tool_input, window):
        return None
    return refusal(window)


def refusal(window) -> str:
    reason = f"BLOCKED: plan artifacts are read only through `{COMMAND}`, which prints this task's chunk with ten lines of margin."
    if window:
        reason += f" Your chunk with its margin is lines {window[1]} to {window[2]}; a read inside them passes."
    return reason


def _read(tool_input: dict, window) -> bool:
    limit = tool_input.get("limit")
    if not isinstance(limit, int) or limit < 1 or PurePath(tool_input.get("file_path") or "").name != window[0]:
        return False
    start = max(int(tool_input.get("offset") or 1), 1)
    return window[1] <= start and start + limit - 1 <= window[2]


def _bash(tool_input: dict, window) -> bool:
    match = SED_RANGE.fullmatch(tool_input.get("command") or "")
    return (
        bool(match)
        and PurePath(match[4]).name == window[0]
        and window[1] <= int(match[2]) <= int(match[3]) <= window[2]
    )


def _never(tool_input: dict, window) -> bool:
    return False


DISPATCH = {"Read": _read, "Grep": _never, "Bash": _bash, "WebFetch": _never}


def _plans(env, slug: str):
    path = Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser() / f"{slug}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), None
    plans = {row["file"]["id"] for row in doc.get("artifacts", []) if row.get("plan") is True}
    return plans, _window(doc, env.get("AGENTIHOOKS_SWARM_TASK", ""))


def _window(doc: dict, task_id: str):
    task = next((t for t in doc.get("tasks", []) if t.get("id") == task_id), {})
    phase = next((p for p in doc.get("phases", []) if p.get("id") == task.get("phase")), {})
    ref, lines = phase.get("plan_ref"), task.get("plan_lines")
    match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", lines) if isinstance(lines, str) and ref else None
    if match is None:
        return None
    return ref["artifact"].rsplit("/", 1)[-1], max(int(match[1]) - MARGIN, 1), int(match[2]) + MARGIN
