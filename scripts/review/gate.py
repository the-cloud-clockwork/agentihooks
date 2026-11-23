"""The review gate: a change request head queues only after an independent review whose findings its author read."""

import argparse
from collections.abc import Callable, Mapping

from scripts.gates.base import Who
from scripts.gates.enforcement import REVIEW
from scripts.review import record
from scripts.review.record import Record
from scripts.swarm import queue_exit
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError


def refusal(found: Record | None, slug: str, url: str, head: str, author: Who) -> str | None:
    if not author.name:
        return f"no author recorded for task {author.task} on head {head[:12]} of {url}"
    if found is None or not found.independent(author.name, author.task):
        return (
            f"no independent review on head {head[:12]} of {url}: a reviewer other than {author.name} "
            "posts its findings with agentihooks review post before this pull request queues"
        )
    unread = found.unread(author.name)
    if unread:
        ids = ", ".join(item.id for item in unread)
        return (
            f"{len(unread)} review findings on head {head[:12]} of {url} are unread: {ids}; "
            f"read each and mark it with agentihooks review read {url} --slug {slug}"
        )
    return None


def admit(store: RedisStore, slug: str, agent: AgentRecord, url: str, head: str, author: Who) -> None:
    reason = refusal(record.read(store, slug, url, head), slug, url, head, author)
    who = Who(name=agent.name, swarm=slug, task=agent.task)
    if reason and REVIEW.allows(store, who, "queue", reason):
        raise SwarmError(reason)


def _author(slug: str, caller: AgentRecord, context: Mapping[str, str] | None) -> Who:
    if context is None:
        return Who(name=caller.name, swarm=slug, task=caller.task)
    from scripts.swarm.ledger_client import LedgerClient

    task = context["task"]
    row: Mapping[str, str | None] = LedgerClient().task(slug, task) or {}  # pyright: ignore[reportUnknownMemberType]
    return Who(name=row.get("claimed_by") or "", swarm=slug, task=task)


def queue_admit(
    store: RedisStore,
    args: argparse.Namespace,
    agent: AgentRecord | None,
    caller: AgentRecord,
    head: str,
    intent: Callable[[RedisStore, argparse.Namespace, AgentRecord, str], None],
    context: Mapping[str, str] | None,
) -> None:
    if agent is not None:
        intent(store, args, agent, head)
    admit(store, args.slug, caller, args.url, head, _author(args.slug, caller, context))
    return queue_exit.require_fresh(args.url, head)  # pyright: ignore[reportUnknownMemberType]
