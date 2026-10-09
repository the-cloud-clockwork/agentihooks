"""Whether the operator is present in a session: a bound session starts off, typed operator on turns it on.

On lasts thirty minutes from the operator's last typed message; operator off ends it at once.
"""

import json
import os
import re
import time
from pathlib import Path

from hooks.context.ledger_decision import _bound

WINDOW_SEC = 1800
QUIET_EVERY = 2
QUIET_WORDS = 20
REMINDER = "Operator not present: replies stay within {words} words."
SWITCH = re.compile(r"operator (on|off)\b")
ON_NOTICE = "Operator on: the operator is present in this pane until thirty minutes after his last typed message."
OFF_NOTICE = "Operator off: the operator is not present in this pane."
TURN_NOTICE = "Operator present for this turn: reply without a word limit."
QUESTION_TOOL = "AskUserQuestion"
ASK_REFUSAL = (
    "The operator is not present in this pane, so the question tool is off. Never write the question as chat text. "
    'Put it on the ledger with agentihooks ledger --slug {slug} --as {name} question add "<the question in plain words>" '
    "and keep working; the master answers it or raises it to the operator."
)
MASTER_ASK_REFUSAL = (
    "The operator is not present in this pane, so the question tool is off. Never write the questions as chat text. "
    "Say only one short line, type operator on to answer the questions here, or leave them in Priorities with "
    'agentihooks ledger --slug {slug} --as {name} priority add <item> "<the ask in plain words>".'
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


def _turn(state, now):
    return state.get("turn_at") is not None and now - state["turn_at"] < WINDOW_SEC


def observe(session_id, prompt, typed, now=None):
    if not session_id:
        return ""
    now = time.time() if now is None else now
    word = switch(prompt) if typed else ""
    state = _load(session_id)
    if word or (typed and _on(state, now)):
        state.update(on=word != "off", at=now)
    _save(session_id, {**state, "turn_at": now if typed and word != "off" else None})
    return word


def notice(payload, typed, now=None):
    session_id = payload.get("session_id")
    word = observe(session_id, payload.get("prompt"), typed, now)
    text = {"on": ON_NOTICE, "off": OFF_NOTICE}.get(word, "")
    if session_id and typed and word != "off" and not present(session_id, now=now):
        return TURN_NOTICE
    if _away(session_id, None, now):
        return "\n".join(filter(None, (text, _reminder_text(None))))
    return text


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
    if tool_name != QUESTION_TOOL or _heard(session_id, env, now):
        return ""
    slug, name = _binding(env, session_id)
    refusal = MASTER_ASK_REFUSAL if name.startswith("master@") else ASK_REFUSAL
    return refusal.format(slug=slug, name=name)


def _heard(session_id, environ, now):
    now = time.time() if now is None else now
    return present(session_id, environ, now) or _turn(_load(session_id), now)


def _away(session_id, environ, now):
    return bool(session_id) and not _heard(session_id, environ, now)


def _reminder_text(environ):
    env = os.environ if environ is None else environ
    return REMINDER.format(words=env.get("AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS", QUIET_WORDS))


def reminder(session_id, environ=None, now=None):
    """The quiet reminder on every second tool call while the operator is away."""
    if not _away(session_id, environ, now):
        return ""
    state = _load(session_id)
    calls = state.get("calls", 0) + 1
    _save(session_id, {**state, "calls": calls})
    return _reminder_text(environ) if calls % QUIET_EVERY == 0 else ""
