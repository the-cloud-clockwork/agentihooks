from typing import Any

from scripts.inbox.store import InboxStore
from scripts.swarm_v2 import master_fence


def reply_fencing_every_sender(store: InboxStore, me: str, record: dict[str, Any]) -> dict[str, Any]:
    args = record["args"]
    sender = store.get(args["id"]).sender
    # ruleid: reply-fences-pooled-item-sender
    master_fence.check_send(store, me, sender)
    answer = store.reply(args["id"], me, args["text"])
    return {"id": answer.id, "to": answer.address, "closed": args["id"]}


def reply_fencing_before_the_pool_check(store: InboxStore, me: str, record: dict[str, Any]) -> dict[str, Any]:
    args = record["args"]
    item = store.get(args["id"])
    # ruleid: reply-fences-pooled-item-sender
    master_fence.check_send(store, me, item.sender)
    if store.reached_through_pool(item, me):
        return {"id": item.id}
    return {"id": store.reply(args["id"], me, args["text"]).id}


def reply_fencing_only_items_outside_the_pool(store: InboxStore, me: str, record: dict[str, Any]) -> dict[str, Any]:
    args = record["args"]
    item = store.get(args["id"])
    if not store.reached_through_pool(item, me):
        # ok: reply-fences-pooled-item-sender
        master_fence.check_send(store, me, item.sender)
    answer = store.reply(args["id"], me, args["text"])
    return {"id": answer.id, "to": answer.address, "closed": args["id"]}


def send_fencing_every_recipient(store: InboxStore, me: str, address: str, text: str) -> dict[str, Any]:
    # ok: reply-fences-pooled-item-sender
    master_fence.check_send(store, me, address)
    item = store.send(me, address, text)
    return {"id": item.id, "to": item.address}
