import json
import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePath

LANES = {"eng", "ci"}
MARGIN = 10
PLAN_ID = re.compile(r"[0-9a-f]{64}\.md")
FOLDER = re.compile(r"\.media\b(?!/[0-9a-f]{64}\.)")
SED_RANGE = re.compile(r"\s*sed\s+-n\s+(['\"]?)([1-9][0-9]*),([1-9][0-9]*)p\1\s+(\S+)\s*")
FIELDS = {"Read": ("file_path",), "Grep": ("path", "glob"), "Bash": ("command",), "WebFetch": ("url",)}
COMMAND = "agentihooks plan read"


def enabled(env: Mapping[str, str]) -> bool:
    return str(env.get("PLAN_READ_GUARD_ENABLED")).strip().lower() not in {"0", "false", "no", "off"}


def check(payload: dict, environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    slug = env.get("AGENTIHOOKS_SWARM")
    if not (enabled(env) and slug and env.get("AGENTIHOOKS_SWARM_LANE") in LANES):
        return None
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if tool_name not in FIELDS or not isinstance(tool_input, dict):
        return None
    try:
        return _decide(env, slug, tool_name, tool_input)
    except Exception:
        return refusal(None)


def _decide(env: Mapping[str, str], slug: str, tool_name: str, tool_input: dict) -> str | None:
    text = _text(tool_name, tool_input)
    folder = _names_folder(text, _ledger_dir(env))
    if not PLAN_ID.search(text) and not folder:
        return None
    plans, window = _plans(env, slug)
    named = set(PLAN_ID.findall(text))
    if not (named & plans or (plans and folder)):
        return None
    if window and named == {window[0]} and DISPATCH[tool_name](tool_input, window):
        return None
    return refusal(window)


def refusal(window: tuple[str, int, int] | None) -> str:
    reason = (
        f"BLOCKED: read plan artifacts through `{COMMAND}`, which prints this task's chunk with ten lines of margin."
    )
    if window:
        reason += (
            f" Your chunk with its margin is lines {window[1]} to {window[2]};"
            " a Read with offset and limit or a `sed -n 'A,Bp'` inside them also passes."
        )
    return reason


def _read(tool_input: dict, window) -> bool:
    limit, offset = tool_input.get("limit"), tool_input.get("offset") or 1
    if type(limit) is not int or limit < 1 or type(offset) is not int:
        return False
    if PurePath(tool_input["file_path"]).name != window[0]:
        return False
    start = max(offset, 1)
    return window[1] <= start and start + limit - 1 <= window[2]


def _bash(tool_input: dict, window) -> bool:
    match = SED_RANGE.fullmatch(_text("Bash", tool_input))
    return (
        bool(match)
        and PurePath(match[4]).name == window[0]
        and window[1] <= int(match[2]) <= int(match[3]) <= window[2]
    )


def _never(tool_input: dict, window) -> bool:
    return False


DISPATCH = {"Read": _read, "Grep": _never, "Bash": _bash, "WebFetch": _never}


def _text(tool_name: str, tool_input: dict) -> str:
    text = " ".join(str(tool_input[field]) for field in FIELDS[tool_name] if tool_input.get(field))
    if tool_name != "Bash":
        return text
    try:
        return " ".join(shlex.split(text))
    except ValueError:
        return text


def _ledger_dir(env) -> Path:
    return Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()


def _names_folder(text: str, root: Path) -> bool:
    if FOLDER.search(text) or ("/artifacts/" in text and "$" in text):
        return True
    for token in text.split():
        path = Path(os.path.expandvars(token)).expanduser()
        if path == root or path.name == root.name:
            return True
        if path.is_relative_to(root) and any(char in token for char in "*?["):
            return True
    return False


def _plans(env, slug: str):
    root = _ledger_dir(env)
    doc = _load(root / f"{slug}.json")
    plans = _plan_ids(doc)
    for path in root.glob("*.json"):
        if path.stem != slug:
            try:
                plans |= _plan_ids(_load(path))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    return plans, _window(doc, env.get("AGENTIHOOKS_SWARM_TASK"))


def _load(path: Path) -> dict:
    return json.loads(path.read_bytes())


def _plan_ids(doc: dict) -> set[str]:
    return {row["file"]["id"] for row in doc.get("artifacts", []) if row.get("plan") is True}


def _window(doc: dict, task_id: str | None):
    task = next((t for t in doc.get("tasks", []) if t.get("id") == task_id), {})
    phase = next((p for p in doc.get("phases", []) if p.get("id") == task.get("phase")), {})
    ref = phase.get("plan_ref")
    match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", str(task.get("plan_lines")))
    if match is None or not ref:
        return None
    return PurePath(ref["artifact"]).name, max(int(match[1]) - MARGIN, 1), int(match[2]) + MARGIN
