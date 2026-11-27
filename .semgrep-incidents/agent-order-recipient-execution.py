from typing import Any

from scripts.inbox.bound import Recipient, recipient_for


def missing_execution(inbox: Any, agent: Any) -> None:
    # ruleid: agent-order-recipient-execution
    inbox.send("operator", agent.name, "Finish this step")


def missing_claimant_execution(inbox: Any, task: Any) -> None:
    # ruleid: agent-order-recipient-execution
    inbox.send("swarm", task["claimed_by"], "Queue your work again")


def missing_named_execution(inbox: Any, claimant: str) -> None:
    # ruleid: agent-order-recipient-execution
    inbox.send("swarm", claimant, "Queue your work again")


def empty_execution(inbox: Any, agent: Any) -> None:
    # ruleid: agent-order-recipient-execution
    inbox.send("operator", Recipient(agent.name, ""), "Finish this step")


def bound_execution(inbox: Any, agent: Any) -> None:
    # ok: agent-order-recipient-execution
    inbox.send("operator", Recipient(agent.name, agent.execution_id), "Finish this step")


def bound_claimant(inbox: Any, store: Any, claimant: str) -> None:
    # ok: agent-order-recipient-execution
    inbox.send("swarm", recipient_for(store, claimant), "Queue your work again")


def unbound_seat_notice(inbox: Any, seat: str) -> None:
    # ok: agent-order-recipient-execution
    inbox.send("operator", seat, "Continue the seat work")
