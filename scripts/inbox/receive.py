from time import monotonic

from scripts.inbox.store import NOTIFY, InboxStore, Item


def receive(store: InboxStore, me: str, timeout: float) -> list[Item]:
    deadline = monotonic() + timeout
    subscription = store.redis.pubsub()
    try:
        subscription.subscribe(NOTIFY)
        while True:
            if items := store.pending_mail(me):
                return items
            remaining = deadline - monotonic()
            if remaining <= 0:
                return []
            subscription.get_message(timeout=remaining)
    finally:
        subscription.close()
