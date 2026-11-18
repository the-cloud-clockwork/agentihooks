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


def stall_alert_with_one_ref_per_task(inbox: InboxStore, watch: Any, address: str, text: str, tid: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", address, text, ref=f"{watch.slug}:stall:{tid}")


def overrun_with_one_ref_per_task(mail: Any, watch: Any, agent: Any, address: str, text: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return mail.inbox.send("swarm", address, text, ref=f"{watch.slug}:overrun:{agent.task}", task=agent.task)


def ask_with_one_ref_per_waiting_task(inbox: InboxStore, victim: Any, text: str, waiting: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", victim.name, text, ref=f"{REF}:{waiting}", task=victim.task)


def notice_with_one_ref_per_task_id(inbox: InboxStore, name: str, text: str, task_id: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=f"notice:{task_id}")


def event_with_one_ref_per_target(mail: Any, key: str, address: str, text: str, target: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return mail.send(key, address, text, ref=f"{mail.slug}:event:{target}")


def event_with_one_ref_per_event_target(mail: Any, key: str, address: str, text: str, event: dict) -> Any:
    # ruleid: inbox-send-fixed-ref
    return mail.send(key, address, text, ref=f"{mail.slug}:event:{event['target']}")


def priority_with_one_ref_per_item(mail: Any, owner: str, text: str, item: str) -> Any:
    # ruleid: inbox-send-fixed-ref
    return mail.inbox.send("swarm", owner, text, ref=f"{mail.slug}:priority:{item}")


def ask_with_its_own_ref(inbox: InboxStore, name: str, text: str, task: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=f"automated-activity:{now_ms}", task=task)


def backstop_with_its_own_ref(inbox: InboxStore, name: str, seat: str, text: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", seat, text, ref=f"{BACKSTOP_REF}:{name}:{now_ms}")


def ask_with_a_constant_and_its_time(inbox: InboxStore, name: str, text: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", name, text, ref=f"{REF}:{now_ms}")


def stall_alert_naming_its_stall(
    inbox: InboxStore, watch: Any, address: str, text: str, tid: str, case: str, since: int
) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", address, text, ref=f"{watch.slug}:stall:{tid}:{case}:{since}:alert")


def overrun_with_the_agent_start(mail: Any, watch: Any, agent: Any, address: str, text: str) -> Any:
    # ok: inbox-send-fixed-ref
    return mail.inbox.send(
        "swarm", address, text, ref=f"{watch.slug}:overrun:{agent.task}:{agent.started_at}", task=agent.task
    )


def ask_for_a_waiting_task_with_its_time(inbox: InboxStore, victim: Any, text: str, waiting: str, now_ms: int) -> Any:
    # ok: inbox-send-fixed-ref
    return inbox.send("swarm", victim.name, text, ref=f"{REF}:{waiting}:{now_ms}", task=victim.task)


def event_with_its_revision(mail: Any, key: str, address: str, text: str, event: dict) -> Any:
    # ok: inbox-send-fixed-ref
    return mail.send(key, address, text, ref=f"{mail.slug}:event:{event['target']}:{event['rev']}")


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
