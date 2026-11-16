from typing import Any

from scripts.inbox.store import InboxStore


def ask_with_one_ref(inbox: InboxStore, name: str, text: str, task: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref="automated-activity", task=task)


def nudge_with_one_ref(inbox: Any, name: str, text: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref="stale-nudge")


def ask_with_its_own_ref(inbox: InboxStore, name: str, text: str, task: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=f"automated-activity:{now_ms}", task=task)


def reply_with_the_question_id(inbox: InboxStore, replier: str, sender: str, text: str, item_id: str) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send(replier, sender, text, ref=item_id)


def send_with_an_empty_ref(inbox: InboxStore, name: str, text: str) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("operator", name, text, ref="")


def send_without_a_ref(inbox: InboxStore, name: str, text: str) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("operator", name, text)
