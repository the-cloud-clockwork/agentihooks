import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from scripts.swarm_ledger import plan_read

SHARED = ("changelog.d",)

Task = dict[str, Any]
Spec = Callable[[Task], str | None]


def code_paths(task: Task) -> list[str]:
    paths = (str(entry).strip("/") for entry in task.get("territory") or ())
    return [path for path in paths if path and path.split("/")[0] not in SHARED]


def overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def names(path: str) -> tuple[str, ...]:
    pure = PurePosixPath(path)
    dotted = ".".join(pure.with_suffix("").parts)
    return (path, pure.name, dotted) if pure.suffix else (path, dotted)


def shares_code(task: Task, dependency: Task) -> bool:
    mine, theirs = code_paths(task), code_paths(dependency)
    return not mine or not theirs or any(overlap(a, b) for a in mine for b in theirs)


def names_code(text: str, dependency: Task) -> bool:
    return any(
        re.search(rf"(?<![\w-]){re.escape(name)}(?!\w)", text)
        for path in code_paths(dependency)
        for name in names(path)
    )


def audit(tasks: list[Task], spec: Spec) -> list[dict[str, str]]:
    rows: dict[str, Task] = {task["id"]: task for task in tasks}
    found: list[dict[str, str]] = []
    for task in tasks:
        if task.get("state") == "done":
            continue
        for dep_id in task.get("depends_on") or ():
            dependency = rows.get(dep_id)
            if dependency is None or dependency.get("state") == "done" or shares_code(task, dependency):
                continue
            text = spec(task)
            if text is not None and not names_code(text, dependency):
                found.append(
                    {
                        "task": task["id"],
                        "title": task.get("title") or task["id"],
                        "depends_on": dep_id,
                        "dependency_title": dependency.get("title") or dep_id,
                    }
                )
    return found


def plan_text(doc: dict[str, Any]) -> Spec:
    def text(task: Task) -> str | None:
        lines = ""
        if task.get("plan_lines"):
            try:
                lines = plan_read.exact(doc, task)  # pyright: ignore[reportUnknownMemberType]
            except Exception:
                return None
        return f"{task.get('description', '')}\n{lines}"

    return text
