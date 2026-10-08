#!/usr/bin/env python3
"""Claude Code hook that keeps a crew member bound to its ledger.

One entry for SessionStart, UserPromptSubmit, PostToolUse and Stop. A session is bound by
`agentihooks ledger join` or `ledger.py join` (learned from the Bash command). Unbound sessions exit at once. Every error fails open.
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))
LEDGER_DIR = Path(os.environ.get("LEDGER_DIR", Path.home() / "development-ledger")).expanduser()
SESSIONS = LEDGER_DIR / ".sessions"
GATE_READS = ("_meta.members", "_meta.events", "tasks", "phases", "followups", "policy")
SHOWN = 5
CLI = "agentihooks ledger"


def log(message):
    import ledger_core as core

    try:
        core.append_capped(LEDGER_DIR / ".hook.log", f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_session(path, data):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def tokens_of(command):
    try:
        return shlex.split(command or "")
    except ValueError:
        return []


def is_ledger_cli(tokens):
    return any(Path(t).name == "ledger.py" for t in tokens) or any(
        a.endswith("agentihooks") and b == "ledger" for a, b in zip(tokens, tokens[1:])
    )


def join_from_command(command):
    tokens = tokens_of(command)
    if not is_ledger_cli(tokens) or "join" not in tokens:
        return None
    opts = {}
    for i, token in enumerate(tokens[:-1]):
        if token in ("--slug", "--as", "--role"):
            opts[token] = tokens[i + 1]
    slug, name = opts.get("--slug"), opts.get("--as")
    return (slug, name, opts.get("--role", "member")) if slug and name else None


def join_from_new(command, response):
    tokens = tokens_of(command)
    if not is_ledger_cli(tokens) or "new" not in tokens:
        return None
    stdout = response.get("stdout", "") if isinstance(response, dict) else str(response or "")
    for text in stdout.splitlines():
        try:
            out = json.loads(text)
        except ValueError:
            continue
        if isinstance(out, dict) and isinstance(out.get("slug"), str) and isinstance(out.get("joined"), str):
            return out["slug"], out["joined"], "member"
    return None


def new_session(slug, name, role):
    return {
        "slug": slug,
        "name": name,
        "role": role,
        "calls": 0,
        "nudged_rev": 0,
        "nudge_calls": 0,
        "blocks": 0,
        "bypass_posted": False,
    }


def bind(payload, sid):
    event = payload.get("hook_event_name")
    found = None
    if (
        event == "PostToolUse"
        and payload.get("tool_name") == "Bash"
        and '"joined"' in str(payload.get("tool_response"))
    ):
        command = (payload.get("tool_input") or {}).get("command")
        found = join_from_command(command) or join_from_new(command, payload.get("tool_response"))
    if found:
        write_session(SESSIONS / f"{sid}.json", new_session(*found))


def context_text(session, owed, extra=""):
    import watch_ledger

    lines = [watch_ledger.line(e) for e in owed[:SHOWN]]
    if len(owed) > SHOWN:
        lines.append(
            f"... and {len(owed) - SHOWN} more (run: {CLI} --slug {session['slug']} --as {session['name']} events)"
        )
    head = (
        f"SWARM LEDGER {session['slug']}: {len(owed)} operator event(s) owe you a reaction."
        if owed
        else f"SWARM LEDGER {session['slug']}:"
    )
    tail = f"Act on them, then run: {CLI} --slug {session['slug']} --as {session['name']} ack" if owed else ""
    return "\n".join(x for x in [head, *lines, extra, tail] if x)


def first_shown(session, owed):
    """The owed events no other path (inbox, watch) has shown this agent yet; all of them when that is unknown."""
    try:
        from scripts.inbox import seen
    except ImportError:
        return owed
    return seen.first_showing(seen.marks_for(session["slug"]), session["name"], session["slug"], owed)


def emit(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def touched(command):
    import ledger_gate

    tokens = tokens_of(command)
    return is_ledger_cli(tokens) and any(t in ledger_gate.WRITE_COMMANDS for t in tokens)


def restart(session):
    session.update(calls=0, nudge_calls=0, blocks=0, bypass_posted=False)


def progress_of(session):
    from scripts.gates.progress import Progress
    from scripts.swarm.store import redis_client

    return Progress(redis_client(), session["slug"])


def note_outcome(session, kind):
    try:
        progress_of(session).outcome(session["name"], kind)
    except Exception as exc:  # the tool call stands whatever Redis does
        log(f"outcome not recorded: {exc}")


def outcome_seen(session):
    try:
        at = progress_of(session).read(session["name"]).outcome_at
    except Exception as exc:  # no signal reads as no outcome
        log(f"progress unreadable: {exc}")
        return False
    if at <= session.get("outcome_at", 0):
        return False
    session["outcome_at"] = at
    restart(session)
    return True


def on_tool(payload, session, state, sfile):
    import ledger_gate

    from scripts.gates.progress import outcome_of

    command = (payload.get("tool_input") or {}).get("command") if payload.get("tool_name") == "Bash" else None
    kind = outcome_of(command)
    if kind:
        note_outcome(session, kind)
    if touched(command) or kind:
        restart(session)
    elif not payload.get("agent_id"):
        session["calls"] += 1
    pol = ledger_gate.policy(state)
    owed = ledger_gate.unhandled_for(state["_meta"], session["name"], state.get("tasks", []))
    top = max((e["rev"] for e in owed), default=0)
    idle = session["calls"] - session["nudge_calls"] >= pol["nudge_after_calls"] and not outcome_seen(session)
    if owed and top > session["nudged_rev"]:
        session["nudged_rev"] = top
        fresh = first_shown(session, owed)
        if fresh:
            emit("PostToolUse", context_text(session, fresh))
    elif idle:
        session["nudge_calls"] = session["calls"]
        note = f"You have made {session['calls']} tool calls without recording progress: update phases, follow-ups, comments or chat."
        emit("PostToolUse", context_text(session, first_shown(session, owed), note))
    write_session(sfile, session)


def on_prompt(payload, session, state, sfile):
    import ledger_gate

    owed = first_shown(session, ledger_gate.unhandled_for(state["_meta"], session["name"], state.get("tasks", [])))
    if owed:
        emit("UserPromptSubmit", context_text(session, owed))


def stop_reasons(session, state):
    import ledger_gate

    pol = ledger_gate.policy(state)
    owed = ledger_gate.unhandled_for(state["_meta"], session["name"], state.get("tasks", []))
    reasons = []
    if owed:
        reasons.append(f"{len(owed)} operator event(s) are unhandled")
    if session["calls"] >= pol["stop_after_calls"] and not outcome_seen(session):
        reasons.append(f"{session['calls']} tool calls since you last recorded progress in the ledger")
    return reasons, len(owed), pol


def post_bypass(session, unhandled):
    try:
        import ledger

        op = {"op": "gate_bypass", "id": f"gb-{uuid.uuid4().hex[:8]}", "by": session["name"], "unhandled": unhandled}
        ledger.request(session["slug"], [op], service=True, timeout=2)
    except Exception as exc:  # the stop must go through whatever the server does
        log(f"bypass not recorded: {exc}")


def on_stop(payload, session, state, sfile):
    reasons, unhandled, pol = stop_reasons(session, state)
    if not reasons or payload.get("permission_mode") == "plan":
        write_session(sfile, session)
        return
    if session["blocks"] >= pol["stop_blocks"]:
        if not session["bypass_posted"]:
            session["bypass_posted"] = True
            write_session(sfile, session)
            post_bypass(session, unhandled)
        return
    session["blocks"] += 1
    write_session(sfile, session)
    fix = f"Handle them and run: {CLI} --slug {session['slug']} --as {session['name']} ack (or record progress with phase/followup/comment/say)."
    print(json.dumps({"decision": "block", "reason": f"SWARM LEDGER {session['slug']}: {'; '.join(reasons)}. {fix}"}))


HANDLERS = {"PostToolUse": on_tool, "UserPromptSubmit": on_prompt, "Stop": on_stop}


def serve_ledgers():
    import ledger_link

    from scripts.swarm_ledger import server_lifetime
    from scripts.swarm_ledger.repository.sqlite import DATABASE

    if os.environ.get("LEDGER_AUTOSTART") == "0" or (
        not (LEDGER_DIR / DATABASE).exists() and not any(LEDGER_DIR.glob("*.json"))
    ):
        return
    try:
        address = ledger_link.address()
        socket.create_connection(address, timeout=0.3).close()
    except OSError:
        subprocess.Popen(
            [sys.executable, str(HERE / "ledger_server.py"), "--ensure"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=server_lifetime.environment(LEDGER_DIR, address[1]),
        )


def dispatch(payload):
    sid = payload.get("session_id")
    if not sid or not SESSIONS.exists() and payload.get("hook_event_name") not in ("SessionStart", "PostToolUse"):
        return
    bind(payload, sid)
    if payload.get("hook_event_name") == "SessionStart":
        serve_ledgers()
    sfile = SESSIONS / f"{sid}.json"
    session = read_json(sfile)
    if session is not None:
        from scripts.swarm.naming import resolve_name

        session["name"] = resolve_name(session["name"])
    handler = HANDLERS.get(payload.get("hook_event_name"))
    if session is None or handler is None:
        return
    import ledger_gate

    from scripts.swarm_ledger.repository.sqlite import read_ledger

    state = read_ledger(LEDGER_DIR, session["slug"], *GATE_READS)
    if not isinstance(state, dict) or not isinstance(state.get("_meta"), dict) or ledger_gate.closed(state):
        return
    if session["name"] not in state["_meta"].get("members", {}):
        return
    handler(payload, session, state, sfile)


def main():
    try:
        payload = json.load(sys.stdin)
        dispatch(payload if isinstance(payload, dict) else {})
    except Exception as exc:  # fail open: a broken hook must never stop an agent
        log(f"hook error: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
