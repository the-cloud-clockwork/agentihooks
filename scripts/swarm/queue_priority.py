import json
import os
import subprocess
import uuid
from collections.abc import Collection
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from scripts.inbox.bound import recipient_for
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import freeze, lab_control, merge_queue, queue_record, ready, staged_compatibility
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_ledger.repository.live_rows import ancestry, covers

DISPLACED = "queue-priority-displaced"
ACTIVE = "queue-priority-active"
ACTIVE_S = 3600
ORDER = "merge-priority-order"


class Displaced:
    def __init__(self, store: RedisStore, slug: str):
        self.redis = store.redis
        self.key = store.key(slug, DISPLACED)
        self.order = store.key(slug, ORDER)

    def add(self, pull: dict) -> None:
        self.redis.hset(self.key, pull["url"], json.dumps({**pull, "order": self.redis.incr(self.order)}))

    def discard(self, url: str) -> None:
        self.redis.hdel(self.key, url)

    def mark(self, pull: dict) -> None:
        self.redis.hset(self.key, pull["url"], json.dumps({**pull, "notified": True}))

    def pulls(self) -> list[dict]:
        found = [json.loads(raw) for raw in self.redis.hgetall(self.key).values()]
        return sorted(found, key=lambda pull: pull["order"])


class Busy(SwarmError):
    pass


def stale(holder: str, hive: str) -> bool:
    parts = holder.rsplit(":", 2)
    return len(parts) == 3 and parts[0] == hive and parts[1].isdigit() and not Path(f"/proc/{parts[1]}").exists()


@contextmanager
def exclusive(store: RedisStore, slug: str):
    from scripts.swarm.commands import hive_id

    hive = hive_id()
    key, token = store.key(slug, ACTIVE), f"{hive}:{os.getpid()}:{uuid.uuid4().hex}"
    holder = store.redis.get(key)
    if holder is not None and stale(holder, hive):
        store.redis.delete(key)
    if not store.redis.set(key, token, nx=True, ex=ACTIVE_S):
        raise Busy("another merge queue priority is running; try again when it ends")
    try:
        yield
    finally:
        if store.redis.get(key) == token:
            store.redis.delete(key)


def owners(tasks: list[dict], urls: Collection[str]) -> list[dict]:
    return [
        task
        for task in tasks
        if task.get("pr_url") in urls
        and task.get("claimed_by")
        and task.get("state") != "done"
        and not ready.awaiting(task)
    ]


def settled(raw: dict | None) -> bool:
    return raw is None or raw["state"] != "OPEN" or raw["baseRefName"] != "dev" or raw["mergeQueueEntry"] is not None


def blocker(raw: dict, head: str) -> str:
    if raw.get("isDraft"):
        return "it became a draft"
    if raw["headRefOid"] != head:
        return "its head moved"
    if raw.get("mergeable") == "CONFLICTING":
        return "it has merge conflicts with dev"
    return ""


def leave_out(store: RedisStore, tasks: list[dict], url: str, head: str, reason: str) -> str:
    queue_record.Journal(url).write("priority-requeue-skipped", url, head, None, None, reason=reason)
    for task in owners(tasks, [url]):
        InboxStore(store.redis).send(
            "swarm",
            recipient_for(store, task["claimed_by"]),
            f"Your pull request {url} left the merge queue in an interrupted priority and stays out "
            f"because {reason}. Queue it again once its checks pass.",
            fyi=True,
        )
    return f"left {url} out of the merge queue after an interrupted priority: {reason}"


def retrying(store: RedisStore, tasks: list[dict], displaced: Displaced, pull: dict, reason: str) -> None:
    claimants = [task["claimed_by"] for task in owners(tasks, [pull["url"]])]
    if pull.get("notified") or not claimants:
        return
    try:
        for claimant in claimants:
            InboxStore(store.redis).send(
                "swarm",
                recipient_for(store, claimant),
                f"Your pull request {pull['url']} left the merge queue in a priority and could not be queued "
                f"again: {reason}. The swarm retries it at the end of the queue on every refresh until it is back "
                "or stays out for a reason.",
                fyi=True,
            )
    except InboxError:
        return
    displaced.mark(pull)


def requeue(store: RedisStore, slug: str, tasks: list[dict], ledger=None) -> list[str]:
    displaced, actions = Displaced(store, slug), []
    for pull in displaced.pulls():
        url, head = pull["url"], pull["headRefOid"]
        try:
            raw = merge_queue.graphql(merge_queue.STATE, {"url": url}, subprocess.run)["resource"]
            if settled(raw):
                displaced.discard(url)
            elif reason := blocker(raw, head):
                actions.append(leave_out(store, tasks, url, head, reason))
                displaced.discard(url)
            else:
                staged_compatibility.require(url, head, raw.get("body") or "", subprocess.run)
                merge_queue.graphql(merge_queue.QUEUE, {"id": pull["id"], "head": head}, subprocess.run)
                displaced.discard(url)
                queue_record.Journal(url).write("priority-requeue", url, head, None, None)
                actions.append(f"requeued {url} left out of the merge queue by an interrupted priority")
        except (SwarmError, InboxError) as exc:
            if ledger is not None:
                ledger.raise_alert(slug, f"merge-priority-requeue:{url}", f"Could not requeue {url}: {exc}")
            actions.append(f"could not requeue {url} after an interrupted priority: {exc}")
            retrying(store, tasks, displaced, pull, str(exc))
    return actions


def apply(
    store: RedisStore,
    slug: str,
    url: str,
    sender: str,
    tasks: list[dict],
    journal: queue_record.Journal,
    lift: bool = False,
) -> dict:
    with exclusive(store, slug):
        return reorder(store, slug, url, sender, tasks, journal, lift=lift)


def reorder(
    store: RedisStore,
    slug: str,
    url: str,
    sender: str,
    tasks: list[dict],
    journal: queue_record.Journal,
    lift: bool,
) -> dict:
    lab_url = store.config(slug).lab_url
    result = merge_queue.prioritize(
        url,
        journal=journal,
        displaced=Displaced(store, slug),
        lift=lift,
        lab=lambda head: lab_control.acknowledge(lab_url, head, os.environ),
    )
    for task in owners(tasks, result["displaced"]):
        InboxStore(store.redis).send(
            sender,
            recipient_for(store, task["claimed_by"]),
            f"Your pull request {task['pr_url']} was requeued in its original order after priority request {url}.",
            fyi=True,
        )
    if result["errors"]:
        requeue(store, slug, tasks)
        raise SwarmError("; ".join(result["errors"]))
    return result


def request(store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
    journal = queue_record.Journal(url)
    if lift:
        journal.write("priority-lift", url, "", None, None)
    return apply(store, slug, url, sender, tasks, journal, lift=lift)


def entitled(ledger: Any, slug: str, tasks: list[dict[str, Any]]) -> bool:
    """Whether a task naming the pull request is urgent or sits under a focus that no nearer freeze overrides."""
    if any(task.get("rank") == "urgent" for task in tasks):
        return True
    records: list[dict[str, Any]] = ledger.freezes(slug)
    focus = [record["target"] for record in records if record.get("verb") == "focus"]
    if not focus:
        return False
    graph = {row["node"]: (row["kind"], row["parent"]) for row in ledger.hierarchy(slug)}
    chains = [(task, ancestry(task, graph)) for task in tasks]
    return any(
        any(covers(target, task, chain) for target in focus) and not freeze.frozen(task, records, chain)
        for task, chain in chains
    )


def automatic(store: RedisStore, slug: str, url: str, ledger: Any) -> dict[str, Any] | None:
    """Move a pull request that just entered the merge queue ahead of the waiting entries when its task is urgent or
    focused, leaving the groups under test in place; None when no such task names it."""
    from redis.exceptions import NoPermissionError

    tasks: list[dict[str, Any]] = ledger.tasks_targeting(slug, url)
    if not entitled(ledger, slug, tasks):
        return None
    try:
        return apply(store, slug, url, "swarm", tasks, queue_record.Journal(url))
    except (SwarmError, NoPermissionError) as exc:
        return {"url": url, "errors": [str(exc)]}


def refresh(store: RedisStore, slug: str, tasks: list[dict], ledger=None) -> list[str]:
    try:
        with exclusive(store, slug):
            return requeue(store, slug, tasks, ledger)
    except Busy as exc:
        return [f"merge queue priority waits: {exc}"]
