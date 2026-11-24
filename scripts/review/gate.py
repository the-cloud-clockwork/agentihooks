import argparse
from collections.abc import Callable, Mapping
from typing import cast

from scripts.gates.base import Who
from scripts.gates.enforcement import REVIEW
from scripts.review import record
from scripts.review.record import Record
from scripts.swarm import queue_exit
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmError


def refusal(found: Record | None, slug: str, url: str, head: str, author: Who) -> str | None:
    if not author.name:
        return f"no author recorded for task {author.task} on head {head[:12]} of {url}"
    if found is None or not any(item.by != author.name and item.task != author.task for item in found.findings):
        return (
            f"no independent review on head {head[:12]} of {url}: a reviewer other than {author.name} "
            "posts its findings with agentihooks review post before this pull request queues"
        )
    unread = tuple(item for item in found.findings if item.state == record.OPEN or author.name not in item.read_by)
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


def _author(slug: str, url: str, caller: AgentRecord, context: Mapping[str, str] | None) -> Who:
    from scripts.swarm.ledger_client import LedgerClient

    if context is not None:
        task = context["task"]
        read_task = cast(Callable[[str, str], Mapping[str, str | None] | None], LedgerClient().task)
        row: Mapping[str, str | None] = read_task(slug, task) or {}
        return Who(name=row.get("claimed_by") or "", swarm=slug, task=task)
    if caller.lane == MASTER:
        rows = LedgerClient().tasks_targeting(slug, url)
        if len(rows) == 1:
            return Who(name=rows[0].get("claimed_by") or "", swarm=slug, task=rows[0]["id"])
        return Who(swarm=slug)
    return Who(name=caller.name, swarm=slug, task=caller.task)


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
    admit(store, args.slug, caller, args.url, head, _author(args.slug, args.url, caller, context))
    require_fresh = cast(Callable[[str, str], None], queue_exit.require_fresh)
    return require_fresh(args.url, head)
