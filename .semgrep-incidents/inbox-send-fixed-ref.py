from typing import Any

from scripts.inbox.store import InboxStore

REF = "master-retire-handoff"
BACKSTOP_REF = "master-backstop"


def ask_with_one_ref(inbox: InboxStore, name: str, text: str, task: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref="automated-activity", task=task)


def nudge_with_one_ref(inbox: Any, name: str, text: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref="stale-nudge")


def ask_with_a_constant_ref(inbox: InboxStore, name: str, text: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=REF)


def ask_with_another_module_constant(inbox: InboxStore, name: str, text: str, retire: Any) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=retire.REF)


def backstop_with_one_ref_per_master(inbox: InboxStore, name: str, seat: str, text: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", seat, text, ref=f"{BACKSTOP_REF}:{name}")


def notice_with_one_ref_per_swarm(mail: Any, agent: Any, text: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return mail.inbox.send("swarm", agent.seat, text, ref=f"stale-nudge:{mail.slug}:{agent.name}")


def alarm_with_one_ref_per_recipient(inbox: InboxStore, master: str, address: str, text: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", address, text, ref=f"step-starvation:{master}:{address}")


def ask_with_its_own_ref(inbox: InboxStore, name: str, text: str, task: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=f"automated-activity:{now_ms}", task=task)


def backstop_with_its_own_ref(inbox: InboxStore, name: str, seat: str, text: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", seat, text, ref=f"{BACKSTOP_REF}:{name}:{now_ms}")


def ask_with_a_constant_and_its_time(inbox: InboxStore, name: str, text: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=f"{REF}:{now_ms}")


def escalate_with_the_item_id(inbox: InboxStore, name: str, text: str, item: Any) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=f"inbox-escalation:{item.id}")


def reply_with_the_question_id(inbox: InboxStore, replier: str, sender: str, text: str, item_id: str) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send(replier, sender, text, ref=item_id)


def send_with_an_empty_ref(inbox: InboxStore, name: str, text: str) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("operator", name, text, ref="")


def send_without_a_ref(inbox: InboxStore, name: str, text: str) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("operator", name, text)
