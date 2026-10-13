from scripts.inbox.outbox import stable_id
from scripts.inbox.seen import write_ref
from scripts.inbox.store import InboxStore
from scripts.swarm_ledger.repository import LedgerRepository

CHAT = "chat"
MESSAGE_ADDED = "message added"
RECEIPT_PREFIX = "received-"
RECEIVED = {"op": "add", "thread": CHAT, "by": "swarm", "to": "operator", "text": "Received by the master inbox."}


def post(repository: LedgerRepository, inbox: InboxStore, slug: str, events: list[dict], addresses: list[str]) -> None:
    ops = []
    for event in events:
        if event.get("target") != CHAT or event.get("kind") != MESSAGE_ADDED:
            continue
        for address in addresses:
            if item := inbox.get(stable_id(write_ref(slug, event), address)):
                ops.append({**RECEIVED, "id": RECEIPT_PREFIX + item.id, "reply_to": event["id"]})
                break
    if ops:
        repository.apply_ops(slug, ops=ops)
