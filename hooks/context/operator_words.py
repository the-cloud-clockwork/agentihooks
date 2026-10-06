"""The operator's own words in an agent session, typed prompts and AskUserQuestion answers, kept one hour.

A relay to the ledger is accepted only when it quotes words recorded here for the relaying agent.
"""

import json
import os
import re
import time

from hooks.context.swarm_heartbeat import is_operator_prompt

TTL_SEC = 3600
KEPT = 50


def _path(name):
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "operator_words" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", name) + ".json")


def _norm(text):
    return " ".join(str(text).lower().split())


def _load(name, now):
    try:
        rows = json.loads(_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and now - r.get("at", 0) < TTL_SEC]


def record(name, words, now=None):
    now = time.time() if now is None else now
    text = str(words or "").strip()
    if not (name and text):
        return False
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps((_load(name, now) + [{"at": now, "words": text}])[-KEPT:]), encoding="utf-8")
    return True


def contains(name, quote, now=None):
    needle = _norm(quote)
    rows = _load(name, time.time() if now is None else now)
    return bool(needle) and any(needle in _norm(r.get("words", "")) for r in rows)


def heard_prompt(prompt, environ=None, now=None):
    env = os.environ if environ is None else environ
    name = env.get("AGENTIHOOKS_AGENT_NAME", "")
    if not name or not is_operator_prompt(prompt or "", env.get("AGENTIHOOKS_SWARM", "")):
        return False
    return record(name, prompt, now)


def _answer_words(payload):
    for source in (payload.get("tool_response"), payload.get("tool_input")):
        if isinstance(source, dict) and source.get("answers"):
            notes = [a.get("notes") for a in (source.get("annotations") or {}).values() if isinstance(a, dict)]
            return "\n".join(str(w) for w in [*source["answers"].values(), *notes] if w)
    return ""


def heard_answer(payload, environ=None, now=None):
    if payload.get("tool_name") != "AskUserQuestion":
        return False
    env = os.environ if environ is None else environ
    return record(env.get("AGENTIHOOKS_AGENT_NAME", ""), _answer_words(payload), now)
