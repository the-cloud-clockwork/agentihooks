from scripts.inbox.store import InboxStore


def resolution(held: dict, rows: dict, inbox: InboxStore) -> str:
    # ruleid: wait-reads-one-inbox-world
    item = inbox.get(held["target"])
    return item.state


def reply_resolution(target: str, tick_inbox: InboxStore) -> str:
    # ruleid: wait-reads-one-inbox-world
    return tick_inbox.get(target).state


def located_resolution(held: dict, rows: dict, inbox: InboxStore) -> tuple:
    # ok: wait-reads-one-inbox-world
    item = inbox.locate(held["target"])
    return rows.get(held["task"]), item.state


def wake(inbox: InboxStore, item_id: str) -> object:
    # ok: wait-reads-one-inbox-world
    return inbox.get(item_id)
