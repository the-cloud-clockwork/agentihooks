"""Decide swarm, small ledger or nothing for a piece of work, and tell the session once."""

import fcntl
import json
import os
import re
import shlex
from contextlib import contextmanager
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME
from hooks.targets import capabilities

STATE_DIR = AGENTIHOOKS_HOME / "ledger-decision"
TASK_LIST_SIZE = 4
TROUBLESHOOT_RE = re.compile(r"\b(?:troubleshoot|debug|investigat|refactor)\w*", re.IGNORECASE)
DECLINE = "agentihooks ledger decline"

SWARM_DIRECTIVE = (
    "LEDGER DECISION: the operator accepted a plan and no ledger is bound to this session. "
    "Start a swarm from that plan now with the init-swarm skill, without asking: it writes the swarm ledger, "
    "creates the swarm and starts it. The swarm's agents implement the plan and its master never edits code, "
    f"so do not implement it here. Only if the operator says no swarm, run `{DECLINE}`."
)
SMALL_DIRECTIVE = (
    "LEDGER DECISION: the operator's rule puts this work on a small ledger ({reason}) and no ledger is bound to "
    "this session. Create it before any other tool call, even for a one line fix: the operator made this call, "
    "do not judge whether the work is small enough to skip it. Write a content file with a title, a one line "
    "overview and one phase per step, then run `agentihooks ledger new --content <file> --slug <short-name> "
    "--size small --as <your name>`, which joins you as its worker. Keep it current: record each step, finding "
    f"and follow-up as it lands. Only if the operator says no ledger, run `{DECLINE}`."
)


def directive(payload, environ=None):
    env = os.environ if environ is None else environ
    session_id = payload.get("session_id") or ""
    kind = _kind(payload)
    if not (kind and session_id) or env.get("AGENTIHOOKS_SWARM") or _bound(env, session_id):
        return ""
    with _session_state(session_id) as state:
        if kind == "decline":
            state["declined"] = True
        if state.get("declined"):
            return ""
        trigger, text = _outcome(kind, payload, state)
        fired = state.setdefault("fired", [])
        if not trigger or trigger in fired or "plan" in fired:
            return ""
        fired.append(trigger)
        return text


def _kind(payload):
    event, tool = payload.get("hook_event_name"), payload.get("tool_name")
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "").strip()
        if prompt in capabilities.plan_accept_prompts():
            return "plan"
        return "prompt" if TROUBLESHOOT_RE.search(prompt) else None
    if event != "PostToolUse":
        return None
    if tool in capabilities.plan_accept_tools():
        return "plan"
    if tool in capabilities.task_list_tools():
        return "list"
    if tool in capabilities.task_add_tools():
        return "add"
    return "decline" if _declined(payload) else None


def _outcome(kind, payload, state):
    if kind == "plan":
        return "plan", SWARM_DIRECTIVE
    if kind == "prompt":
        return "prompt", SMALL_DIRECTIVE.format(
            reason="the operator asked to troubleshoot, debug, investigate or refactor"
        )
    if kind == "list":
        count = len((payload.get("tool_input") or {}).get("todos") or [])
    else:
        state["tasks_added"] = count = state.get("tasks_added", 0) + 1
    if count < TASK_LIST_SIZE:
        return None, ""
    return "tasks", SMALL_DIRECTIVE.format(reason=f"the session's task list has {count} items")


def _declined(payload):
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return False
    try:
        tokens = shlex.split((payload.get("tool_input") or {}).get("command") or "")
    except ValueError:
        return False
    response = payload.get("tool_response")
    stdout = response.get("stdout", "") if isinstance(response, dict) else str(response or "")
    return " ".join(tokens) == DECLINE and '"declined": true' in stdout


def _bound(env, session_id):
    ledgers = Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
    return (ledgers / ".sessions" / f"{session_id}.json").exists()


@contextmanager
def _session_state(session_id):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session_id}.json"
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        yield state
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
