"""Mail a hive member sends to an address outside its own keys waits there, because the member's Redis user writes only
below its own hive key; the controller moves each such item to its address's inbox and refuses one whose sender that
member does not run."""

from scripts.hive import scope
from scripts.inbox.store import DuplicateItem, InboxStore

RELAYER = "inbox"
RELAYED = "relayed to its address's inbox"
FOREIGN_SENDER = "refused: the hive member that sent it does not run its sender"


def relay(redis) -> list[str]:
    """The ids of the member items sent onwards."""
    central = InboxStore(redis, world="")
    sent = []
    for member in scope.members(redis):
        world = InboxStore(redis, world=member)
        for address in sorted(redis.smembers(world.key("waiting"))):
            if world.home(address) == member:
                continue
            for item in world.pending_items(address):
                if world.home(item.sender) != member:
                    world.withdraw(item.id, RELAYER, FOREIGN_SENDER)
                    continue
                try:
                    central.send(item.sender, address, item.text, item.ref, item.fyi, item.task, item.id)
                    sent.append(item.id)
                except DuplicateItem:
                    pass
                world.withdraw(item.id, RELAYER, RELAYED)
    return sent
