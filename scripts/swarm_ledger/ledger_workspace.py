"""A swarm task's work folder: steering notes seeded from the task, and the progress and proof its agents append."""

import bisect
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.swarm_ledger import plan_read, ports

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from redis import Redis

PART_RE = re.compile(r"[A-Za-z0-9][\w.-]{0,127}")
CONTRACT_LABELS = (("must", "Must be true"), ("check", "Checked by"), ("judge", "Judged by"))
TAILS = (("latest_progress", "progress.md"), ("latest_proof", "proof.md"))
TAIL_LINES = 3
TAIL_BYTES = 4096
SEEDS = ("steering.md", "progress.md", "proof.md", "recon.md")
SEED_CHARS = 65536
LINE_BYTES = 131072
SEED_BYTES = 2 * LINE_BYTES
FILES = 32
FILES_SECONDS = 86400
ENDED = ("ended", "finished")
WORK_FOLDER = "work-folder"


def folder(slug: str, task_id: str) -> Path:
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


def steering(task: dict[str, Any], doc: dict[str, Any] | None = None) -> str:
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
    if task.get("enhances") and doc is not None:
        from scripts.swarm.prompt import target_lines

        lines += target_lines(task, doc["tasks"])
    if task.get("kind") == "plan" and doc is not None:
        from scripts.swarm_ledger.plan_evidence import evidence

        lines.append(evidence(task, doc))
    return "\n".join(lines)


def rewrite(task: dict) -> None:
    if not Path(task["workspace"]).is_dir():
        return
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


def portable(slug, task_id):
    folder(slug, task_id)
    return f"~/.agentihooks/swarm/{slug}/tasks/{task_id}"


def named(slug, agent, recorded=""):
    if ports.hive().reports.host(agent):
        return portable(slug, agent.task)
    return recorded or str(folder(slug, agent.task))


def seeds(slug, task_id):
    steering = folder(slug, task_id) / "steering.md"
    sent = {"steering.md": steering.read_text(encoding="utf-8")} if steering.is_file() else {}
    return {**sent, **contents(slug, task_id)}


def contents(slug: str, task_id: str) -> dict[str, str]:
    """Progress, proof and the recon plan keep their latest LINE_BYTES of JSON; another plain text file of up to
    SEED_CHARS bytes travels whole while its JSON fits what is left of SEED_BYTES, so the hub admit reply carrying the
    copy stays under the NATS payload limit."""
    found: dict[str, str] = {}
    added, left = 0, SEED_BYTES
    for file in _files(folder(slug, task_id)):
        seed = file.name in SEEDS
        if not seed and added >= FILES:
            break
        try:
            if not seed and file.stat().st_size > SEED_CHARS:
                continue
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        kept = _latest(text, LINE_BYTES if seed else left)
        if seed or kept == text:
            found[file.name] = kept
            left -= _json_bytes(kept)
            added += not seed
    return found


def _latest(text: str, limit: int) -> str:
    """The longest end of text whose JSON takes at most limit bytes; a character takes at least one byte, so only the
    last limit characters are searched."""
    tail = text[max(len(text) - limit, 0) :]

    def fits(start: int) -> bool:
        return _json_bytes(tail[start:]) <= limit

    return tail[bisect.bisect_left(range(len(tail)), True, key=fits) :]


def _json_bytes(text: str) -> int:
    """The bytes text adds to a JSON message, which escapes every character outside ASCII."""
    return len(json.dumps(text)) - 2


def _files(path):
    try:
        listed = sorted(path.iterdir(), key=lambda file: (file.name not in SEEDS, file.name))
    except OSError:
        return []
    kept = [file for file in listed if file.name != "steering.md" and PART_RE.fullmatch(file.name)]
    return [file for file in kept if file.is_file() and not file.is_symlink()]


def materialize(slug, task_id, sent):
    """Steering follows the controller; a file the hive holds stays its own only while it already holds what was sent."""
    path = folder(slug, task_id)
    path.mkdir(parents=True, exist_ok=True)
    for name, text in sent.items():
        if not PART_RE.fullmatch(name) or (path / name).is_symlink():
            continue
        if name != "steering.md" and _holds(path / name, text):
            continue
        (path / name).write_text(text, encoding="utf-8")
    return path


def _holds(path, text):
    try:
        return text in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def synced_key(hive):
    return f"{ports.hive().registry.key(hive)}:workspaces"


def files_key(hive):
    return f"{synced_key(hive)}:files"


def task_files_key(hive: str, slug: str, task_id: str) -> str:
    """The key under the hive's own prefix that holds its latest copy of one task's work folder files."""
    return f"{files_key(hive)}:{slug}:{task_id}"


def sync(redis: "Redis", hive: str, hosted: list[tuple[str, str]], ttl: int) -> None:
    """Publishes the hosted tasks' latest lines and files, and keeps each task's files under its own key with no expiry,
    so the copy seeds the task's next agent however long after this hive stops hosting the task; a task holding no files
    leaves its copy as it was."""
    copy: dict[str, dict[str, dict[str, str]]] = {}
    files: dict[str, dict[str, dict[str, str]]] = {}
    for slug, task_id in hosted:
        copy.setdefault(slug, {})[task_id] = tails(slug, task_id)
        found = contents(slug, task_id)
        files.setdefault(slug, {})[task_id] = found
        if found:
            redis.set(task_files_key(hive, slug, task_id), json.dumps(found))
    redis.set(synced_key(hive), json.dumps(copy), ex=ttl)
    redis.set(files_key(hive), json.dumps(files), ex=FILES_SECONDS)


def launched_on(store: Any, slug: str, task_id: str, hive: str) -> None:
    """Records the hive that last launched the task, whose task copy seeds the task's next agent."""
    store.redis.set(store.key(slug, WORK_FOLDER, task_id), hive)


def latest(agents: "Iterable[Any]") -> "dict[str, Any]":
    """Each task's live execution, else the one that started last."""
    ordered = sorted(agents, key=lambda agent: (agent.state not in ENDED, agent.started_at))
    return {agent.task: agent for agent in ordered}


def published(store: Any, slug: str, ledger: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Each task's latest work folder lines: the synced copy of the hive that runs, or last ran, its execution, else the
    lines held here, which the mirror keeps from that hive's task copy once the hive stops publishing them."""
    copies: dict[str, Any] = {}

    def synced(host: str, task_id: str) -> dict[str, str] | None:
        if host not in copies:
            copies[host] = json.loads(store.redis.get(synced_key(host)) or "{}")
        return copies[host].get(slug, {}).get(task_id)

    return {task_id: copy or tails(slug, task_id) for task_id, copy in _synced(store, slug, ledger, synced).items()}


def mirror(store: Any, slug: str, ledger: dict[str, Any]) -> None:
    """A task run on another hive takes that hive's task copy here, which outlives the hive, so a successor on any host
    and the page read its files."""

    def kept(host: str, task_id: str) -> dict[str, str]:
        return json.loads(store.redis.get(task_files_key(host, slug, task_id)) or "{}")

    for task_id, copy in _synced(store, slug, ledger, kept).items():
        if not copy:
            continue
        path = folder(slug, task_id)
        path.mkdir(parents=True, exist_ok=True)
        for name, text in copy.items():
            if name != "steering.md" and PART_RE.fullmatch(name) and not (path / name).is_symlink():
                (path / name).write_text(text, encoding="utf-8")


def _synced(
    store: Any, slug: str, ledger: dict[str, Any], read: "Callable[[str, str], dict[str, str] | None]"
) -> dict[str, Any]:
    """Each work folder task's copy, read from the hive that runs, or last ran, its execution; None for one run here."""
    ran = latest(store.agents(slug))
    found: dict[str, Any] = {}
    for task in ledger.get("tasks", []):
        if not task.get("workspace"):
            continue
        host = ports.hive().reports.workspace_host(ran[task["id"]]) if task["id"] in ran else ""
        found[task["id"]] = read(host, task["id"]) if host else None
    return found
