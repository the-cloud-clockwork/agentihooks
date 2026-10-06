"""Whether the operator is present in a session: a ledger or swarm bound session starts off, typed operator on turns it on.

On lasts thirty minutes from the operator's last typed message; operator off ends it at once.
"""

import json
import os
import re
import time

from hooks.context.ledger_decision import _bound

WINDOW_SEC = 1800
SWITCH = re.compile(r"operator (on|off)\b")
ON_NOTICE = "Operator on: the operator is present in this pane until thirty minutes after his last typed message."
OFF_NOTICE = "Operator off: the operator is not present in this pane."


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
    if word or _on(_load(session_id), now):
        _save(session_id, {"on": word != "off", "at": now})
    return word


def present(session_id, environ=None, now=None):
    env = os.environ if environ is None else environ
    if not (env.get("AGENTIHOOKS_SWARM") or _bound(env, session_id)):
        return True
    return _on(_load(session_id), time.time() if now is None else now)
