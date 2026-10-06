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
        data = json.loads(_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    rows = [r for r in data.get("rows", []) if now - r["at"] < TTL_SEC]
    return {"sessions": data.get("sessions", []), "rows": rows}


def _save(name, data):
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def record(name, words, now=None):
    now = time.time() if now is None else now
    text = str(words or "").strip()
    if not (name and text):
        return False
    data = _load(name, now)
    data["rows"] = (data["rows"] + [{"at": now, "words": text}])[-KEPT:]
    _save(name, data)
    return True


def matching(name, quote, now=None):
    """The latest recorded words that hold the quote, or an empty string."""
    needle = _norm(quote)
    rows = _load(name, time.time() if now is None else now)["rows"] if needle else []
    return next((r["words"] for r in reversed(rows) if needle in _norm(r["words"])), "")


def _opening(name, session, now):
    """True once per session: the first prompt of a swarm agent is its launch or handoff prompt."""
    data = _load(name, now)
    if not session or session in data["sessions"]:
        return False
    data["sessions"] = (data["sessions"] + [session])[-KEPT:]
    _save(name, data)
    return True


def heard_prompt(prompt, environ=None, now=None, session=""):
    env = os.environ if environ is None else environ
    name, slug = env.get("AGENTIHOOKS_AGENT_NAME", ""), env.get("AGENTIHOOKS_SWARM", "")
    if not name or not is_operator_prompt(prompt or "", slug):
        return False
    now = time.time() if now is None else now
    if slug and _opening(name, session, now):
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


def heard(payload, environ=None, now=None):
    if "prompt" in payload:
        return heard_prompt(payload["prompt"], environ, now, payload.get("session_id", ""))
    return heard_answer(payload, environ, now)
