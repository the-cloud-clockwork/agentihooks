"""Whether the operator is present in a session: a bound session starts off, typed operator on turns it on.

The question tool and permission prompts stay off in a bound session until typed operator on; an unbound session always has them.

On lasts until the operator types operator off.
"""

import json
import os
import re
import time
from pathlib import Path

from hooks.context.ledger_decision import _bound

QUIET_EVERY = 2
QUIET_WORDS = 20
REMINDER = "Operator not present: replies stay within {words} words."
SWITCH = re.compile(r"(?<![\w-])operator (on|off)(?![\w-])")
ON_NOTICE = "Operator on: the operator is present in this pane until he types operator off or the session restarts."
OFF_NOTICE = "Operator off: the operator is not present in this pane."
REPLY_REFUSAL = (
    "Operator not present: this reply has {count} words, over the {words} word limit. "
    "Reply again in at most {words} words, machine style: result only, no explanation."
)
REPLY_RETRIES = 3
MARKER = re.compile(r"<!--\s*@(\w+).*?<!--\s*@/\1\s*-->|<!--.*?-->", re.DOTALL)
QUESTION_TOOL = "AskUserQuestion"
ASK_REFUSAL = (
    "The operator is not present in this pane, so the question tool is off. Put the question on the ledger with "
    'agentihooks ledger --slug {slug} --as {name} question add "<the question in plain words>" '
    "and keep working; the master answers it or raises it to the operator."
)
MASTER_ASK_REFUSAL = (
    "The operator is not present in this pane, so the question tool is off. Never write the questions as chat text. "
    "Say only one short line, type operator on to answer the questions here, or leave them in Priorities with "
    'agentihooks ledger --slug {slug} --as {name} priority add <item> "<the ask in plain words>".'
)
PROMPT_REFUSAL = (
    "Denied {tool}: the operator is not present in this pane, and only his typed operator on opens this prompt. "
    "Keep working without it."
)


def _path(session_id):
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "operator_mode" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", session_id) + ".json")


def _load(session_id):
    try:
        state = json.loads(_path(session_id).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save(session_id, state):
    path = _path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _on(state, now):
    return bool(state.get("on"))


def switch(prompt):
    words = SWITCH.findall(" ".join(str(prompt).lower().split()))
    return "off" if "off" in words else "on" if words else ""


def observe(session_id, prompt, typed, now=None):
    from hooks.context import operator_expiry

    if not session_id:
        return ""
    now = time.time() if now is None else now
    word = switch(prompt) if typed else ""
    with operator_expiry.state_lock(session_id):
        state = _load(session_id)
        if word or (typed and _on(state, now)):
            state.update(on=word != "off", at=now)
        state["turn_at"] = now if typed and word != "off" else None
        operator_expiry.cancel(session_id, state)
    return word


def reset(payload: dict) -> None:
    from hooks.context import operator_expiry

    session_id = payload.get("session_id")
    if not session_id or payload.get("source") == "compact":
        return
    with operator_expiry.state_lock(session_id):
        state = _load(session_id)
        state.update(on=False, turn_at=None)
        operator_expiry.cancel(session_id, state)


def notice(payload, typed, now=None):
    session_id = payload.get("session_id")
    word = observe(session_id, payload.get("prompt"), typed, now)
    text = {"on": ON_NOTICE, "off": OFF_NOTICE}.get(word, "")
    if _away(session_id, None, now):
        return "\n".join(filter(None, (text, _reminder_text(None))))
    return text


def present(session_id, environ=None, now=None):
    env = os.environ if environ is None else environ
    if not _attached(env, session_id):
        return True
    return _on(_load(session_id), time.time() if now is None else now)


def _attached(env, session_id):
    return bool(env.get("AGENTIHOOKS_SWARM") or _bound(env, session_id))


def _binding(env, session_id):
    try:
        ledgers = Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
        bound = json.loads((ledgers / ".sessions" / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        bound = {}
    slug = bound.get("slug") or env.get("AGENTIHOOKS_SWARM") or "<slug>"
    return slug, bound.get("name") or env.get("AGENTIHOOKS_AGENT_NAME") or "<name>"


def question_block(tool_name, session_id, environ=None):
    """The refusal of the question tool until the operator types operator on, naming what to do instead."""
    env = os.environ if environ is None else environ
    if tool_name != QUESTION_TOOL or _heard(session_id, env):
        return ""
    slug, name = _binding(env, session_id)
    refusal = MASTER_ASK_REFUSAL if name.startswith("master@") else ASK_REFUSAL
    return refusal.format(slug=slug, name=name)


def _heard(session_id, environ=None):
    env = os.environ if environ is None else environ
    return not _attached(env, session_id) or (bool(session_id) and bool(_load(session_id).get("on")))


def _away(session_id, environ, now):
    return bool(session_id) and not present(session_id, environ, now)


def _limit(environ):
    env = os.environ if environ is None else environ
    return int(env.get("AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS", QUIET_WORDS))


def _reminder_text(environ):
    return REMINDER.format(words=_limit(environ))


def reply_refusal(payload, environ=None):
    """The refusal of a final reply over the word limit while the operator is away, at most REPLY_RETRIES per stop."""
    from hooks.context import operator_expiry

    session_id = payload.get("session_id")
    if not _away(session_id, environ, None):
        return ""
    count = len(MARKER.sub(" ", str(payload.get("last_assistant_message") or "")).split())
    words = _limit(environ)
    if count <= words:
        return ""
    with operator_expiry.state_lock(session_id):
        state = _load(session_id)
        refused = state.get("refusals", 0) + 1 if payload.get("stop_hook_active") else 1
        _save(session_id, {**state, "refusals": refused})
    return REPLY_REFUSAL.format(count=count, words=words) if refused <= REPLY_RETRIES else ""


def reminder(session_id, environ=None, now=None):
    """The quiet reminder on every second tool call while the operator is away."""
    from hooks.context import operator_expiry

    if not session_id:
        return ""
    with operator_expiry.state_lock(session_id):
        if not _away(session_id, environ, now):
            return ""
        state = _load(session_id)
        calls = state.get("calls", 0) + 1
        _save(session_id, {**state, "calls": calls})
    return _reminder_text(environ) if calls % QUIET_EVERY == 0 else ""
