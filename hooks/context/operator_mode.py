"""Whether the operator is present in a session: a bound session starts off, typed operator on turns it on.

On lasts thirty minutes from the operator's last typed message; operator off ends it at once.
"""

import json
import os
import re
import time
from pathlib import Path

from hooks.context.ledger_decision import _bound
from scripts.swarm_ledger.ledger_gate import DEFAULT_POLICY

WINDOW_SEC = 1800
QUIET_EVERY = 2
QUIET_WORDS = 10
STOP_CAP = DEFAULT_POLICY["stop_blocks"]
REMINDER = "Operator not present: replies stay under ten words."
QUIET_REFUSAL = "The operator is not present: your final message has {words} words. Say it in ten words or fewer."
SWITCH = re.compile(r"operator (on|off)\b")
ON_NOTICE = "Operator on: the operator is present in this pane until thirty minutes after his last typed message."
OFF_NOTICE = "Operator off: the operator is not present in this pane."
QUESTION_TOOL = "AskUserQuestion"
ASK_REFUSAL = (
    "The operator is not present in this pane, so the question tool is off. Put the question on the ledger with "
    'agentihooks ledger --slug {slug} --as {name} question add "<the question in plain words>" '
    "and keep working; the master answers it or raises it to the operator."
)


def _path(session_id):
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "operator_mode" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", session_id) + ".json")


def _load(session_id):
    try:
        return json.loads(_path(session_id).read_text())
    except (OSError, ValueError):
        return {}


def _save(session_id, state):
    path = _path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _on(state, now):
    return bool(state.get("on")) and now - state["at"] < WINDOW_SEC


def switch(prompt):
    found = SWITCH.match(" ".join(str(prompt).lower().split()))
    return found.group(1) if found else ""


def observe(session_id, prompt, typed, now=None):
    """Apply one prompt to the session's mode and return the switch it carried; only a typed prompt counts."""
    if not (session_id and typed):
        return ""
    now = time.time() if now is None else now
    word = switch(prompt)
    state = _load(session_id)
    if word or _on(state, now):
        _save(session_id, {**state, "on": word != "off", "at": now})
    return word


def notice(payload, typed, now=None):
    """The line a prompt that switched the mode tells the session, or an empty string."""
    word = observe(payload.get("session_id"), payload.get("prompt"), typed, now)
    return {"on": ON_NOTICE, "off": OFF_NOTICE}.get(word, "")


def present(session_id, environ=None, now=None):
    env = os.environ if environ is None else environ
    if not (env.get("AGENTIHOOKS_SWARM") or _bound(env, session_id)):
        return True
    return _on(_load(session_id), time.time() if now is None else now)


def _binding(env, session_id):
    try:
        ledgers = Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
        bound = json.loads((ledgers / ".sessions" / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        bound = {}
    slug = bound.get("slug") or env.get("AGENTIHOOKS_SWARM") or "<slug>"
    return slug, bound.get("name") or env.get("AGENTIHOOKS_AGENT_NAME") or "<name>"


def question_block(tool_name, session_id, environ=None, now=None):
    """The refusal of the question tool while the operator is away, naming the ledger command to use instead."""
    env = os.environ if environ is None else environ
    if tool_name != QUESTION_TOOL or present(session_id, env, now):
        return ""
    slug, name = _binding(env, session_id)
    return ASK_REFUSAL.format(slug=slug, name=name)


def _away(session_id, environ, now):
    return bool(session_id) and not present(session_id, environ, now)


def reminder(session_id, environ=None, now=None):
    """The one line quiet reminder, on the first and every second tool call while the operator is away."""
    if not _away(session_id, environ, now):
        return ""
    state = _load(session_id)
    calls = state.get("calls", 0) + 1
    _save(session_id, {**state, "calls": calls})
    return REMINDER if calls % QUIET_EVERY == 1 else ""


def quiet_block(session_id, message, environ=None, now=None):
    """The Stop refusal of a final message over ten words while the operator is away; the cap lets it through."""
    if not _away(session_id, environ, now):
        return ""
    state = _load(session_id)
    words = len(str(message or "").split())
    blocks = state.get("blocks", 0)
    if words <= QUIET_WORDS or blocks >= STOP_CAP:
        if blocks:
            _save(session_id, {**state, "blocks": 0})
        return ""
    _save(session_id, {**state, "blocks": blocks + 1})
    return QUIET_REFUSAL.format(words=words)
