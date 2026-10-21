"""Tell a swarm-bound session to add an accepted plan as new ledger phases."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME
from hooks.targets import capabilities

STATE_DIR = AGENTIHOOKS_HOME / "ledger-decision"

PHASES_DIRECTIVE = (
    "LEDGER DECISION: the operator accepted a plan and this session is bound to the ledger {slug}, so the plan "
    "continues that ledger. Add it there as new phases now, without asking: write the plan's phases list in the "
    "init swarm shape to a file, then run `agentihooks ledger --slug {slug} --as {name} plan phases <phases.json>`. "
    "The phases land planned manually and in review. Never start a new swarm for this plan, and do not implement "
    "it here."
)


def directive(payload, environ=None):
    env = os.environ if environ is None else environ
    session_id = payload.get("session_id") or ""
    if not session_id or not _is_plan_accept(payload):
        return ""
    binding = _binding(env, session_id)
    if not binding:
        return ""
    with _session_state(session_id) as state:
        fired = state.setdefault("fired", [])
        if "plan" in fired:
            return ""
        fired.append("plan")
        return PHASES_DIRECTIVE.format(**binding)


def _is_plan_accept(payload):
    event, tool = payload.get("hook_event_name"), payload.get("tool_name")
    if event == "UserPromptSubmit":
        return str(payload.get("prompt") or "").strip() in capabilities.plan_accept_prompts()
    return event == "PostToolUse" and tool in capabilities.plan_accept_tools()


def _bound(env, session_id):
    return _session_file(env, session_id).exists()


def _session_file(env, session_id):
    return _ledger_dir(env) / ".sessions" / f"{session_id}.json"


def _ledger_dir(env):
    return Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()


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
