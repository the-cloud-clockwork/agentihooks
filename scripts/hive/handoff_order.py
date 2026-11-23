"""The hand off orders a hive drain or release roll sends its agents, and the check that knows one."""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.inbox.store import Item

SENDER = "operator"
ORDER = (
    "The operator is removing hive {hive}, which runs you. Finish the step you are on, then hand off with the "
    "handoff skill: run agentihooks swarm {slug} handoff <doc> --reason operator and stop. The runtime starts your "
    "successor on another hive."
)
ROLL = (
    "Hive {hive}, which runs you, is stopping for a release and takes no new agents. Finish the step you are on, commit "
    "and push your branch so no work is lost with the pod, then hand off with the handoff skill even while you wait on checks, a merge or a reply, naming that wait under Next: "
    "run agentihooks swarm {slug} handoff <doc> --reason operator and stop. The runtime starts your successor on "
    "another hive."
)
ORDERS = tuple(
    re.compile(re.escape(text).replace(r"\{hive\}", ".+").replace(r"\{slug\}", ".+")) for text in (ORDER, ROLL)
)


def is_order(item: "Item") -> bool:
    return item.sender == SENDER and any(pattern.fullmatch(item.text) for pattern in ORDERS)
