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
STORED_PROBE = "title"
TROUBLESHOOT_RE = re.compile(r"\b(?:troubleshoot|debug|investigat|refactor)\w*", re.IGNORECASE)
DECLINE = "agentihooks ledger decline"

SWARM_DIRECTIVE = (
    "LEDGER DECISION: the operator accepted a plan and no ledger is bound to this session. "
    "Start a swarm from that plan now with the init-swarm skill, without asking: it writes the swarm ledger, "
    "creates the swarm and starts it. The swarm's agents implement the plan and its master never edits code, "
    f"so do not implement it here. Only if the operator says no swarm, run `{DECLINE}`."
)
PHASES_DIRECTIVE = (
    "LEDGER DECISION: the operator accepted a plan and this session is bound to the ledger {slug}, so the plan "
    "continues that ledger. Add it there as new phases now, without asking: write the plan's phases list in the "
    "init swarm shape to a file, then run `agentihooks ledger --slug {slug} --as {name} plan phases <phases.json>`. "
    "The phases land planned manually and in review. Never start a new swarm for this plan, and do not implement "
    "it here."
)
OFFER_DIRECTIVE = (
    "LEDGER DECISION: the operator accepted a plan and no ledger is bound to this session, but the plan names the "
    "existing ledger {slug}. Offer that ledger first: ask the operator whether the plan continues {slug}. If it "
    "does, write the plan's phases list in the init swarm shape to a file, then run `agentihooks ledger --slug "
    "{slug} --as {name} plan phases <phases.json>`; the phases land planned manually and in review. If it does not, "
    "start a swarm from the plan with the init-swarm skill. Do not implement the plan here. Only if the operator "
    f"says no ledger and no swarm, run `{DECLINE}`."
)
TOKEN_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
SMALL_DIRECTIVE = (
    "LEDGER DECISION: the operator's rule puts this work on a small ledger ({reason}) and no ledger is bound to "
    "this session. Create it before any other tool call, even for a one line fix: the operator made this call, "
    "do not judge whether the work is small enough to skip it. Write a content file with a title, a one line "
    "overview and one phase per step, then run `agentihooks ledger new --content <file> --size small "
    "--as <your name>`, which names the ledger from this session and joins you as its worker. Keep it current: record each step, finding "
    f"and follow-up as it lands. Only if the operator says no ledger, run `{DECLINE}`."
)


def directive(payload, environ=None):
    env = os.environ if environ is None else environ
    session_id = payload.get("session_id") or ""
    kind = _kind(payload)
    if not (kind and session_id):
        return ""
    binding = _binding(env, session_id)
    if binding and kind != "plan":
        return ""
    with _session_state(session_id) as state:
        if kind == "decline":
            state["declined"] = True
        if state.get("declined"):
            return ""
        trigger, text = _outcome(kind, payload, state, binding, env)
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


def _outcome(kind, payload, state, binding, env):
    if kind == "plan":
        return "plan", _plan_directive(payload, binding, env)
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


def _plan_directive(payload, binding, env):
    if binding:
        return PHASES_DIRECTIVE.format(**binding)
    slug = _named_ledger(payload, env)
    if not slug:
        return SWARM_DIRECTIVE
    return OFFER_DIRECTIVE.format(slug=slug, name=env.get("AGENTIHOOKS_AGENT_NAME") or "<your name>")


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
    return _session_file(env, session_id).exists()


def _session_file(env, session_id):
    return _ledger_dir(env) / ".sessions" / f"{session_id}.json"


def _ledger_dir(env):
    return Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()


def _named_ledger(payload, env):
    from scripts.swarm_ledger.repository.sqlite import read_ledger, read_registry

    ledgers = _ledger_dir(env)
    binned = read_registry(ledgers, "bin")
    for slug in TOKEN_RE.findall(_plan_text(payload).lower()):
        if slug not in binned and read_ledger(ledgers, slug, STORED_PROBE) is not None:
            return slug
    return ""


def _plan_text(payload):
    tool_input = payload.get("tool_input") or {}
    try:
        return Path(tool_input["planFilePath"]).read_bytes().decode()
    except (KeyError, TypeError, OSError, ValueError):
        return str(tool_input.get("plan") or tool_input.get("summary") or "")


def _binding(env, session_id):
    if not (_bound(env, session_id) or env.get("AGENTIHOOKS_SWARM")):
        return None
    try:
        bound = dict(json.loads(_session_file(env, session_id).read_text()))
    except (OSError, ValueError, TypeError):
        bound = {}
    return {
        "slug": bound.get("slug") or env.get("AGENTIHOOKS_SWARM") or "<slug>",
        "name": bound.get("name") or env.get("AGENTIHOOKS_AGENT_NAME") or "<your name>",
    }


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
