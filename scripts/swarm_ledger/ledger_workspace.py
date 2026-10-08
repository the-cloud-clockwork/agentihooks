"""A swarm task's work folder: steering notes seeded from the task, and the progress and proof its agents append."""

import re
from pathlib import Path

from scripts.swarm_ledger import plan_read

PART_RE = re.compile(r"[A-Za-z0-9][\w.-]{0,127}")
CONTRACT_LABELS = (("must", "Must be true"), ("check", "Checked by"), ("judge", "Judged by"))
TAILS = (("latest_progress", "progress.md"), ("latest_proof", "proof.md"))
TAIL_LINES = 3
TAIL_BYTES = 4096


def folder(slug, task_id):
    if not all(isinstance(part, str) and PART_RE.fullmatch(part) for part in (slug, task_id)):
        raise ValueError(f"work folder needs a plain slug and task id, not {slug!r} and {task_id!r}")
    return Path.home() / ".agentihooks" / "swarm" / slug / "tasks" / task_id


def scaffold(slug: str, task: dict, doc: dict | None = None) -> Path:
    path = folder(slug, task["id"])
    path.mkdir(parents=True, exist_ok=True)
    for name, text in (("steering.md", steering(task, doc)), ("progress.md", ""), ("proof.md", "")):
        if name == "steering.md" and task.get("kind") == "plan" and doc is not None:
            (path / name).write_text(text, encoding="utf-8")
            continue
        try:
            with (path / name).open("x", encoding="utf-8") as fh:
                fh.write(text)
        except FileExistsError:
            pass
    return path


def steering(task: dict, doc: dict | None = None) -> str:
    lines = [f"# {task['id']}: {task.get('title', '')}", ""]
    if task.get("description"):
        lines += [task["description"], ""]
    if task.get("plan_lines"):
        lines += [plan_read.pointer(task), ""]
    elif task.get("plan_url"):
        lines += [f"Plan: {task['plan_url']}", ""]
    contract = task.get("contract") or {}
    rows = [f"- {label}: {contract[key]}" for key, label in CONTRACT_LABELS if contract.get(key)]
    if rows:
        lines += ["Proof contract", *rows, ""]
    if task.get("kind") == "plan" and doc is not None:
        from scripts.swarm_ledger.plan_evidence import evidence

        lines.append(evidence(task, doc))
    return "\n".join(lines)


def rewrite(task: dict) -> None:
    (Path(task["workspace"]) / "steering.md").write_text(steering(task), encoding="utf-8")


def tails(slug, task_id):
    path = folder(slug, task_id)
    return {key: "\n".join(lines) for key, name in TAILS if (lines := _tail(path / name))}


def _tail(path):
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(size - TAIL_BYTES, 0))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    if size > TAIL_BYTES:
        text = text.split("\n", 1)[-1]
    return [line for line in text.splitlines() if line.strip()][-TAIL_LINES:]
