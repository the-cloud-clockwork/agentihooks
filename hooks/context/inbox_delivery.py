import os

from hooks.targets import is_codex_memory_thread
from scripts.inbox.cli import identity
from scripts.inbox.seen import claim
from scripts.inbox.store import InboxError, connect, now_ms

CLOSE_HINT = "done|handoff <address>|blocked <what>|cancel [why]"


def pending_context(session_id, environ=None, cwd=""):
    if cwd and is_codex_memory_thread(cwd):
        return ""
    started = now_ms()
    receiver = _receiver(session_id, environ)
    if receiver is None:
        return ""
    me, store = receiver
    store.confirm_shown(me, started)
    return "\n\n".join(_render(item) for item in claim(store, me))


def confirm_shown(session_id, environ=None):
    started = now_ms()
    receiver = _receiver(session_id, environ)
    if receiver is not None:
        me, store = receiver
        store.confirm_shown(me, started)


def _receiver(session_id, environ):
    env = dict(os.environ if environ is None else environ)
    env["CLAUDE_CODE_SESSION_ID"] = env.get("CLAUDE_CODE_SESSION_ID") or session_id
    try:
        return identity(env), connect(env)
    except InboxError:
        return None


def _render(item):
    return (
        f"=== INBOX: message {item.id} from {item.sender} ===\n{item.text}\n"
        f"Answer it: agentihooks msg reply {item.id} <text>, or close it: agentihooks msg close {item.id} {CLOSE_HINT}"
    )
