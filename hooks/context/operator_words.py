"""The operator's own words in an agent session, typed prompts and AskUserQuestion answers, the latest ROWS_KEPT.

A relay to the ledger is accepted only when it quotes words recorded here for a master or planner of its swarm.
"""

import json
import os
import re
import time

from hooks.context.swarm_heartbeat import is_operator_prompt

TTL_SEC = 3600
KEPT = 50
ROWS_KEPT = 500


def _path(name):
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "operator_words" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", name) + ".json")


def _norm(text):
    return " ".join(str(text).lower().split())


def _load(name):
    try:
        data = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        data = {}
    return {"sessions": data.get("sessions", []), "rows": data.get("rows", [])}


def recorded(pattern):
    """Names with words recorded under the glob `pattern`."""
    return sorted(p.stem for p in _path("x").parent.glob(f"{pattern}.json"))


def _save(name, data):
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def record(name, words, now=None):
    now = time.time() if now is None else now
    text = str(words or "").strip()
    if not (name and text):
        return False
    data = _load(name)
    data["rows"] = (data["rows"] + [{"at": now, "words": text}])[-ROWS_KEPT:]
    _save(name, data)
    return True


def matching(name, quote, now=None, within=TTL_SEC):
    """The latest words recorded under `within` seconds ago, or ever when it is None, that hold the quote."""
    needle = _norm(quote)
    now = time.time() if now is None else now
    rows = [r for r in _load(name)["rows"] if within is None or now - r["at"] < within] if needle else []
    return next((r["words"] for r in reversed(rows) if needle in _norm(r["words"])), "")


def _opening(name, session):
    """True once per session: the first prompt of a swarm agent is its launch or handoff prompt."""
    data = _load(name)
    if not session or session in data["sessions"]:
        return False
    data["sessions"] = (data["sessions"] + [session])[-KEPT:]
    _save(name, data)
    return True


def heard_prompt(prompt, environ=None, now=None, session=""):
    env = os.environ if environ is None else environ
    name, slug = env.get("AGENTIHOOKS_AGENT_NAME"), env.get("AGENTIHOOKS_SWARM")
    if not name or not is_operator_prompt(prompt, slug):
        return False
    now = time.time() if now is None else now
    if slug and _opening(name, session):
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
    return record(env.get("AGENTIHOOKS_AGENT_NAME"), _answer_words(payload), now)


def heard(payload, environ=None, now=None):
    if "prompt" in payload:
        return heard_prompt(payload["prompt"], environ, now, payload.get("session_id"))
    return heard_answer(payload, environ, now)


def typed(payload, environ=None, now=None):
    """True when the payload holds the operator's own words: a named session records them, an unnamed one has no launch prompt."""
    env = os.environ if environ is None else environ
    if env.get("AGENTIHOOKS_AGENT_NAME"):
        return heard(payload, env, now)
    return is_operator_prompt(payload.get("prompt", ""), env.get("AGENTIHOOKS_SWARM"))
