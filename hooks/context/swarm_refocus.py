import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME
from scripts.swarm_ledger import plan_read

STATE_DIR = AGENTIHOOKS_HOME / "swarm-refocus"
DEFAULT_EVERY = 40
DEFAULT_MAX_CHARS = 1500


def build_block(ledger, task_id, cap):
    if task_id == "master":
        return _master_block(ledger, cap)
    task = next((t for t in ledger.get("tasks", []) if t.get("id") == task_id), None)
    if not task:
        return ""
    phase = next((p for p in ledger.get("phases", []) if p.get("id") == task.get("phase")), {})
    share = cap // 5
    head = (
        f"=== SWARM REFOCUS: {ledger.get('title', '')} ===\n"
        f"Plan: {_clip(ledger.get('overview', ''), share)}\n"
        f"Phase: {phase.get('title', '')}: {_clip(phase.get('description', ''), share)}\n"
        f"Your task {task_id}: {task.get('title', '')}\n"
        + (f"{plan_read.pointer(task)}\n" if task.get("plan_lines") else "")
    )
    return _clip(head + task.get("description", ""), cap)


def _master_block(ledger: dict, cap: int) -> str:
    share = cap // 5
    phases = "; ".join(
        f"{phase.get('title', '')}: {phase.get('description', '')}"
        for phase in ledger.get("phases", [])
        if not phase.get("done")
    )
    priorities = "; ".join(item.get("text", "") for item in ledger.get("priorities", []))
    return _clip(
        f"=== SWARM REFOCUS: {_clip(ledger.get('title', ''), share)} ===\n"
        "Master obligations: Troubleshoot with read only diagnostics, plan with the operator, "
        "configure the swarm, the ledger and the operator's environment with him through the agentihooks commands "
        "and tools, handle operator inbox items, keep the ledger current and judge progress; "
        "never edit code or config files in a repository, commit, merge or claim a task.\n"
        f"Plan: {_clip(ledger.get('overview', ''), share)}\n"
        f"Priorities: {_clip(priorities, share)}\n"
        f"Active phases: {_clip(phases, share * 2)}",
        cap,
    )


def refocus_context(session_id, event, environ=None):
    env = os.environ if environ is None else environ
    binding = _binding(env)
    if not (binding and session_id):
        return ""
    ledger = _read_json(_ledger_dir(env) / f"{binding[0]}.json")
    block = (
        build_block(ledger, binding[1], _int(env, "AGENTIHOOKS_REFOCUS_MAX_CHARS", DEFAULT_MAX_CHARS)) if ledger else ""
    )
    if not block:
        return ""
    digest = hashlib.sha256(block.encode()).hexdigest()
    every = _int(env, "AGENTIHOOKS_REFOCUS_EVERY", DEFAULT_EVERY)
    with _session_state(session_id) as state:
        state["calls"] = state.get("calls", 0) + (event == "tool")
        due = (
            state.get("compacted")
            or state.get("hash") != digest
            or (event == "tool" and state["calls"] - state.get("at", 0) >= every)
        )
        if due:
            state.update(hash=digest, at=state["calls"], compacted=False)
    return block if due else ""


def mark_compacted(session_id, environ=None):
    if not (_binding(os.environ if environ is None else environ) and session_id):
        return
    with _session_state(session_id) as state:
        state["compacted"] = True


def _binding(env):
    slug, task_id = env.get("AGENTIHOOKS_SWARM", ""), env.get("AGENTIHOOKS_SWARM_TASK", "")
    return (slug, task_id) if slug and task_id else None


def _ledger_dir(env):
    return Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()


def _int(env, name, default):
    try:
        return max(1, int(env.get(name) or default))
    except ValueError:
        return default


def _clip(text, limit):
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@contextmanager
def _session_state(session_id):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session_id}.json"
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_json(path) or {}
        yield state
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
