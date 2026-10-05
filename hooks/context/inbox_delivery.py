import os

from hooks.targets import is_codex_memory_thread
from scripts.inbox.cli import identity
from scripts.inbox.store import InboxError, connect

CLOSE_HINT = "done|handoff <address>|blocked <what>|cancel [why]"


def pending_context(session_id, environ=None, cwd=""):
    if cwd and is_codex_memory_thread(cwd):
        return ""
    env = dict(os.environ if environ is None else environ)
    env["CLAUDE_CODE_SESSION_ID"] = env.get("CLAUDE_CODE_SESSION_ID") or session_id
    try:
        me = identity(env)
        store = connect(env)
    except InboxError:
        return ""
    delivered = [store.deliver(item.id, me) for item in store.pending_mail(me)]
    return "\n\n".join(_render(item) for item in delivered if item)


def _render(item):
    return (
        f"=== INBOX: message {item.id} from {item.sender} ===\n{item.text}\n"
        f"Answer it: agentihooks msg reply {item.id} <text>, or close it: agentihooks msg close {item.id} {CLOSE_HINT}"
    )
