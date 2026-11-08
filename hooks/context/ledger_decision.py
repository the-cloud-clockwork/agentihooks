"""Tell a swarm-bound session to ingest an accepted plan into its ledger with agentihooks plan ingest."""

import fcntl
import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hooks.config import AGENTIHOOKS_HOME
from hooks.context import worker_mode
from hooks.targets import capabilities
from scripts.swarm_ledger.ledger_phases import PLANNING_DEFAULT

STATE_DIR = AGENTIHOOKS_HOME / "ledger-decision"
PLAN_LANE = "plan"
PLAN_PLACEHOLDER = "<plan file>"

INGEST_DIRECTIVE = (
    "LEDGER DECISION: the operator accepted a plan and this session is bound to the ledger {slug}, so the plan "
    "continues that ledger. Ingest it there now, without asking: {prepare}run "
    "`agentihooks plan ingest {path} --ledger {slug}`. Its phases land planned {planning} unless the plan says "
    "otherwise. Never start a new swarm for this plan, and do not implement it here."
)
WRITE_STEP = "write the accepted plan as markdown to a file, then "


def directive(payload: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> str:
    """Return the one-shot plan ingest directive for a bound session accepting a plan, else an empty string."""
    env = os.environ if environ is None else environ
    session_id = payload.get("session_id") or ""
    if not session_id or worker_mode.active(env) or env.get("AGENTIHOOKS_SWARM_LANE") == PLAN_LANE:
        return ""
    if not _is_plan_accept(payload):
        return ""
    binding = _binding(env, session_id)
    if not binding:
        return ""
    with _session_state(session_id) as state:
        fired = state.setdefault("fired", [])
        if "plan" in fired:
            return ""
        fired.append("plan")
    path = _plan_path(payload, session_id)
    return INGEST_DIRECTIVE.format(
        slug=binding["slug"],
        path=path,
        planning=PLANNING_DEFAULT,
        prepare=WRITE_STEP if path == PLAN_PLACEHOLDER else "",
    )


def _plan_path(payload: Mapping[str, Any], session_id: str) -> str:
    """Name the accepted plan's file: the payload's own path, a scratch copy of its text, or a placeholder."""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    carried = tool_input.get("planFilePath")
    if isinstance(carried, str) and carried:
        return carried
    text = tool_input.get("plan")
    if not isinstance(text, str) or not text.strip():
        return PLAN_PLACEHOLDER
    scratch = STATE_DIR / f"{session_id}.plan.md"
    scratch.write_bytes(text.encode())
    return str(scratch)


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
