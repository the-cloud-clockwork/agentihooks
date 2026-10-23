"""A swarm task's work folder: steering notes seeded from the task, and the progress and proof its agents append."""

import json
import re
from pathlib import Path

from scripts.swarm_ledger import plan_read, ports

PART_RE = re.compile(r"[A-Za-z0-9][\w.-]{0,127}")
CONTRACT_LABELS = (("must", "Must be true"), ("check", "Checked by"), ("judge", "Judged by"))
TAILS = (("latest_progress", "progress.md"), ("latest_proof", "proof.md"))
TAIL_LINES = 3
TAIL_BYTES = 4096
SEEDS = ("steering.md", "progress.md", "proof.md")
SEED_CHARS = 65536
ENDED = ("ended", "finished")


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
    path = Path(task["workspace"]).expanduser()
    if path.is_dir():
        (path / "steering.md").write_text(steering(task), encoding="utf-8")


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


def portable(slug, task_id):
    folder(slug, task_id)
    return f"~/.agentihooks/swarm/{slug}/tasks/{task_id}"


def named(slug, agent, recorded=""):
    if ports.hive().reports.host(agent):
        return portable(slug, agent.task)
    return recorded or str(folder(slug, agent.task))


def seeds(slug, task_id):
    path = folder(slug, task_id)
    sent = {name: (path / name).read_text(encoding="utf-8") for name in SEEDS if (path / name).is_file()}
    return {name: text if name == "steering.md" else text[-SEED_CHARS:] for name, text in sent.items()}


def materialize(slug, task_id, sent):
    """Steering follows the controller; progress and proof the hive already holds stay its own."""
    path = folder(slug, task_id)
    path.mkdir(parents=True, exist_ok=True)
    for name in SEEDS:
        if name not in sent:
            continue
        if name == "steering.md":
            (path / name).write_text(sent[name], encoding="utf-8")
            continue
        try:
            with (path / name).open("x", encoding="utf-8") as fh:
                fh.write(sent[name])
        except FileExistsError:
            pass
    return path


def synced_key(hive):
    return f"{ports.hive().registry.key(hive)}:workspaces"


def sync(redis, hive, hosted, ttl):
    copy = {}
    for slug, task_id in hosted:
        copy.setdefault(slug, {})[task_id] = tails(slug, task_id)
    redis.set(synced_key(hive), json.dumps(copy), ex=ttl)


def latest(agents):
    """Each task's live execution, else the one that started last."""
    ordered = sorted(agents, key=lambda agent: (agent.state not in ENDED, agent.started_at))
    return {agent.task: agent for agent in ordered}


def published(store, slug, ledger):
    """Each task's latest work folder lines: the synced copy of the hive that runs, or last ran, its execution."""
    ran = latest(store.agents(slug))
    copies = {}
    found = {}
    for task in ledger.get("tasks", []):
        if not task.get("workspace"):
            continue
        host = ports.hive().reports.host(ran[task["id"]]) if task["id"] in ran else ""
        if not host:
            found[task["id"]] = tails(slug, task["id"])
            continue
        if host not in copies:
            copies[host] = json.loads(store.redis.get(synced_key(host)) or "{}")
        found[task["id"]] = copies[host].get(slug, {}).get(task["id"], {})
    return found
