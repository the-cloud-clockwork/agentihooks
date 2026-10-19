import os

from hooks.targets import is_codex_memory_thread
from scripts.inbox.cli import identity
from scripts.inbox.seen import claim
from scripts.inbox.store import InboxError, connect, now_ms
from scripts.inbox.wake import window_ms

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
    messages = [_render(item) for item in claim(store, me)]
    if reminder := _settlement_context(store, me, started, os.environ if environ is None else environ):
        messages.append(reminder)
    return "\n\n".join(messages)


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
        store = connect(env)
        store.session_id = session_id
        return identity(env), store
    except InboxError:
        return None


def _render(item):
    instruction = (
        "Information only; closes on read or confirmed receipt."
        if item.fyi
        else f"Answer it: agentihooks msg reply {item.id} <text>, or close it: agentihooks msg close {item.id} {CLOSE_HINT}"
    )
    return f"=== INBOX: message {item.id} from {item.sender} ===\n{item.text}\n{instruction}"


def _settlement_context(store, me, started, environ):
    window = window_ms(environ)
    items = store.unsettled(me, started - window)
    if not items:
        return ""
    key = store.key("settlement-reminder", store.names.resolve(me))
    if not store.redis.set(key, "shown", nx=True, px=window):
        return ""
    return (
        "=== INBOX: awaiting explicit settlement ===\n"
        f"Confirmed or read work items: {', '.join(item.id for item in items)}.\n"
        "After handling each item, use agentihooks msg reply <id> <text> or "
        "agentihooks msg close <id> done <outcome> to name where the work went. "
        "Keep unresolved work and proof checks open; receipt alone does not settle work."
    )
