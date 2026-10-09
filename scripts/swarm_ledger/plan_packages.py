import re
from pathlib import Path

from scripts.swarm_ledger import plan_ranges

PLAN = Path(__file__).resolve().parents[2] / "Swarm-v2.md"
PACKAGE = re.compile(r"(?<![\w.-])SV2-[A-Z]+-\d{2}(?![\w.-])")
ISSUE = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/\d+/?")


def name(task: dict) -> str:
    if task.get("plan_slice"):
        return task["plan_slice"]
    names = set(PACKAGE.findall(task.get("description", "")))
    if len(names) > 1:
        raise ValueError("task description names more than one Swarm v2 package")
    return next(iter(names), task["id"])


def linked(name: str, url: str) -> bool:
    return bool(PACKAGE.fullmatch(name) and ISSUE.fullmatch(url))


def text() -> str:
    return PLAN.read_text(encoding="utf-8")


def shared_lines(source: str) -> str:
    phases = [
        {"id": match[1], "title": title}
        for _, depth, title in plan_ranges.sections(source)
        if depth == 2 and (match := re.match(r"([1-3])\.\s", title))
    ]
    if [p["id"] for p in phases] != ["1", "2", "3"]:
        raise ValueError("Swarm v2 plan needs ordered shared sections 1 to 3")
    ranges = plan_ranges.phase_lines(source, phases)
    return f"{plan_ranges.bounds(ranges['1'])[0]}-{plan_ranges.bounds(ranges['3'])[1]}"


def read(lines: str) -> str:
    from scripts.swarm_ledger import plan_read

    source = text()
    return plan_read.chunk(source, shared_lines(source), margin=0) + "\n" + plan_read.chunk(source, lines)
