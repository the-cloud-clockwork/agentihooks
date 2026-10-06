"""The build gate: a swarm engineer or CI agent edits and commits only inside its traced plan and task territory.

Areas are repo-relative path prefixes: the kept pieces of a passing trace-plan verdict for the current plan.md, and the
task's territory on the ledger. Every edit before that verdict is refused. The task work folder, ~/scratchpad and a
file outside any git work tree are exempt. An unchecked verdict lets every edit through, counted in the gate log.
"""

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from scripts.gates import log
from scripts.gates.base import Decision
from scripts.gates.identity import program_index, simple_commands
from scripts.swarm import naming, trace_plan
from scripts.swarm_ledger import ledger_workspace

LANES = frozenset({"eng", "ci"})
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SERENA = "mcp__serena__"
SERENA_EDITS = frozenset(
    {
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
        "replace_content",
        "replace_in_files",
        "rename_symbol",
        "safe_delete_symbol",
        "create_text_file",
    }
)
PATCH_START = "*** Begin Patch"
PATCH_TARGET = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.MULTILINE)
ALL_SHORT = re.compile(r"^-[A-Za-z]*a[A-Za-z]*$")
GIT_TIMEOUT_SEC = 5
SHOWN = 5


def _area(entry):
    return str(entry).strip().removeprefix("./").rstrip("/")


def within(path, areas):
    return any(path == area or path.startswith(area + "/") for area in map(_area, areas) if area)


def git_root(path):
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def task_territory(ledger_dir, slug, task):
    try:
        doc = json.loads((Path(ledger_dir) / f"{slug}.json").read_text())
    except (OSError, ValueError):
        return []
    row = next((t for t in doc.get("tasks", []) if isinstance(t, dict) and t.get("id") == task), {})
    return list(row.get("territory") or [])


def _git_commits(command, cwd):
    where = Path(cwd or ".")
    for words in simple_commands(command):
        index = program_index(words)
        if index is None:
            continue
        program, rest = PurePosixPath(words[index]).name, iter(words[index + 1 :])
        if program == "cd":
            where = where / next(rest, str(Path.home()))
            continue
        if program != "git":
            continue
        directory = where
        for word in rest:
            if word == "-C":
                directory = directory / next(rest, "")
            elif word == "-c":
                next(rest, "")
            elif not word.startswith("-"):
                if word == "commit":
                    args = list(rest)
                    yield directory, any(a == "--all" or ALL_SHORT.match(a) for a in args)
                break


def _git_names(directory, *args):
    out = subprocess.run(
        ["git", "-C", str(directory), "diff", "--name-only", "-z", *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SEC,
    )
    return [name for name in out.stdout.split("\0") if name] if out.returncode == 0 else []


def _patch_targets(call, key):
    body = str(call.tool_input.get("content") or "")
    found = [target.strip() for target in PATCH_TARGET.findall(body)] if PATCH_START in body else []
    return found or [str(call.tool_input.get(key) or "")]


def plan_areas(folder, who):
    """('deny', reason), ('unchecked', []) or ('pass', the kept pieces' areas)."""
    trace = f"agentihooks swarm {who.swarm} trace-plan"
    try:
        pieces = trace_plan.parse((folder / trace_plan.PLAN).read_text(), who.task)
    except OSError:
        return (
            "deny",
            f"build gate: no traced plan yet. Write {folder / trace_plan.PLAN}, {trace_plan.FORMAT}, then run {trace}",
        )
    except ValueError as exc:
        return "deny", f"build gate: {exc}, then run {trace}"
    record = trace_plan.load(folder)
    if record.get("plan_hash") != trace_plan.plan_hash(pieces):
        return "deny", f"build gate: plan.md has no verdict for its current text. Run {trace}"
    if record.get("verdict") == trace_plan.UNCHECKED:
        return "unchecked", []
    if record.get("verdict") != trace_plan.PASS:
        reasons = " and ".join(record.get("reasons") or []) or "no passing verdict"
        return "deny", f"build gate: the plan failed its trace: {reasons}. Revise plan.md and run {trace}"
    return "pass", [area for row in record.get("pieces", []) if row.get("kept") for area in row.get("areas", [])]


def refusal(who, outside, areas, plan):
    shown = ", ".join(outside[:SHOWN]) + (f" and {len(outside) - SHOWN} more" if len(outside) > SHOWN else "")
    return (
        f"build gate: outside your traced plan: {shown}. Kept areas and territory: {', '.join(areas) or 'none'}. "
        f"If the task needs it, append a piece `{trace_plan.FORMAT.split(': ', 1)[1]}` to {plan} and run "
        f"agentihooks swarm {who.swarm} trace-plan; otherwise propose it: agentihooks ledger --slug {who.swarm} "
        f'--as {who.name} followup add "<text>"'
    )


class BuildGate:
    name = "build"
    default_mode = "observe"

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ

    def matches(self, call):
        if call.tool in EDIT_TOOLS:
            return True
        if call.tool.startswith(SERENA):
            return call.tool[len(SERENA) :] in SERENA_EDITS
        return call.tool == "Bash" and "commit" in call.command and any(_git_commits(call.command, call.cwd))

    def touched(self, call, folder):
        if call.tool.startswith(SERENA):
            return [] if call.tool_input.get("dry_run") else [_area(call.tool_input.get("relative_path") or "")]
        if call.tool == "Bash":
            names = []
            for directory, everything in _git_commits(call.command, call.cwd):
                names += _git_names(directory, "--cached") + (_git_names(directory, "HEAD") if everything else [])
            return sorted(set(names))
        exempt = [folder.resolve(), (Path.home() / "scratchpad").resolve()]
        found = []
        for named in filter(None, _patch_targets(call, EDIT_TOOLS[call.tool])):
            path = (Path(call.cwd or ".") / named).resolve()
            root = git_root(path)
            if root is None or any(path == ex or ex in path.parents for ex in exempt):
                continue
            found.append(path.relative_to(root).as_posix())
        return found

    def decide(self, call, who, state):
        if not (who.pinned and who.task) or naming.lane_of(who.name) not in LANES:
            return Decision()
        folder = ledger_workspace.folder(who.swarm, who.task)
        paths = self.touched(call, folder)
        if not paths:
            return Decision()
        status, value = plan_areas(folder, who)
        if status == "deny":
            return Decision.deny(value)
        if status == "unchecked":
            reason = f"unchecked plan, edit allowed: {', '.join(paths[:SHOWN])}"
            log.append(state.slug, log.Row.of(self.name, "count", who, call.tool, reason), state.home)
            return Decision()
        outside = [path for path in paths if not within(path, value)]
        if outside:
            ledger_dir = Path(self.environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
            territory = task_territory(ledger_dir, who.swarm, who.task)
            value = value + territory
            outside = [path for path in outside if not within(path, territory)]
        if outside:
            return Decision.deny(refusal(who, outside, value, folder / trace_plan.PLAN))
        return Decision()
