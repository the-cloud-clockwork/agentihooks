import json
import os
import subprocess
import uuid
from collections.abc import Collection
from contextlib import contextmanager
from pathlib import Path

from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import merge_queue, queue_record, ready, staged_compatibility
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, SwarmError

PENDING = "queue-priority"
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
            task["claimed_by"],
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
                claimant,
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


def blocking(url: str, journal: queue_record.Journal, head: str | None = None) -> dict | None:
    groups = journal.groups()
    if groups is None:
        raise SwarmError("queue priority waits until the head group can be read")
    if head is None:
        if not groups or not groups[0]["head"]:
            return None
        head = groups[0]["head"]
    repo = "/".join(url.split("/")[3:5])
    runs = merge_queue.rest(
        f"repos/{repo}/actions/workflows/test.yml/runs?event=merge_group&head_sha={head}&per_page=100",
        subprocess.run,
    )["workflow_runs"]
    if runs and runs[0]["status"] == "completed":
        return None
    if not runs and not any(group["head"] == head for group in groups):
        return None
    return {"repo": repo, "group_head": head, "run_id": runs[0]["id"] if runs else None}


def apply(
    store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], journal: queue_record.Journal, ahead=()
) -> dict:
    with exclusive(store, slug):
        return reorder(store, slug, url, sender, tasks, journal, ahead)


def reorder(
    store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], journal: queue_record.Journal, ahead=()
) -> dict:
    result = merge_queue.prioritize(
        url, journal=journal, displaced=Displaced(store, slug), **({"ahead": ahead} if ahead else {})
    )
    for task in owners(tasks, result["displaced"]):
        InboxStore(store.redis).send(
            sender,
            task["claimed_by"],
            f"Your pull request {task['pr_url']} was requeued in its original order after priority request {url}.",
            fyi=True,
        )
    if result["errors"]:
        requeue(store, slug, tasks)
        raise SwarmError("; ".join(result["errors"]))
    store.redis.hdel(store.key(slug, PENDING), url)
    return result


def request(store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
    journal = queue_record.Journal(url)
    held = None if lift else blocking(url, journal)
    if held is not None:
        record = {**held, "sender": sender, "order": store.redis.incr(store.key(slug, ORDER))}
        store.redis.hset(store.key(slug, PENDING), url, json.dumps(record))
        journal.write("priority-deferred", url, "", None, None, **record)
        return {"url": url, "deferred": True, "displaced": [], "errors": [], **held}
    if lift:
        journal.write("priority-lift", url, "", None, None)
    return apply(store, slug, url, sender, tasks, journal)


def drop_reason(url: str) -> str:
    raw = merge_queue.graphql(merge_queue.STATE, {"url": url}, subprocess.run)["resource"]
    if raw is None:
        raise SwarmError("pull request could not be read")
    if raw["state"] != "OPEN":
        return f"pull request is {raw['state']}"
    if raw["baseRefName"] != "dev":
        return "pull request no longer targets dev"
    if raw.get("isDraft"):
        return "pull request is a draft"
    return ""


def replay(
    store: RedisStore,
    slug: str,
    url: str,
    record: dict,
    tasks: list[dict],
    journal: queue_record.Journal,
    ledger: LedgerClient | None,
) -> None:
    try:
        reorder(store, slug, url, record["sender"], tasks, journal, record.get("ahead"))
    except SwarmError as exc:
        journal.write("priority-failed", url, "", None, None, errors=[str(exc)], **record)
        if ledger is not None:
            ledger.raise_alert(slug, f"merge-priority:{url}", f"Deferred merge priority failed for {url}: {exc}")
        raise


def refresh(store: RedisStore, slug: str, tasks: list[dict], ledger=None) -> list[str]:
    try:
        with exclusive(store, slug):
            return requeue(store, slug, tasks, ledger) + pending(store, slug, tasks, ledger)
    except Busy as exc:
        return [f"merge queue priority waits: {exc}"]


def pending(store: RedisStore, slug: str, tasks: list[dict], ledger) -> list[str]:
    actions = []
    records = [(url, json.loads(raw)) for url, raw in store.redis.hgetall(store.key(slug, PENDING)).items()]
    records.sort(key=lambda item: item[1].get("order", 0))
    while records:
        url, record = records.pop(0)
        try:
            journal = queue_record.Journal(url)
            reason = drop_reason(url)
            if reason:
                store.redis.hdel(store.key(slug, PENDING), url)
                journal.write("priority-dropped", url, "", None, None, reason=reason, **record)
                actions.append(f"dropped deferred merge queue priority for {url}: {reason}")
                continue
            if record["run_id"] is None:
                held = blocking(url, journal, record["group_head"])
                if held is not None:
                    store.redis.hset(store.key(slug, PENDING), url, json.dumps({**record, **held}))
                    continue
            else:
                run = merge_queue.rest(f"repos/{record['repo']}/actions/runs/{record['run_id']}", subprocess.run)
                if run["status"] != "completed":
                    continue
            replay(store, slug, url, record, tasks, journal, ledger)
            for later_url, later in records:
                later["ahead"] = list(dict.fromkeys([*record.get("ahead", []), *later.get("ahead", []), url]))
                store.redis.hset(store.key(slug, PENDING), later_url, json.dumps(later))
            journal.write("priority-applied", url, "", None, None, **record)
            actions.append(f"applied deferred merge queue priority for {url}")
        except SwarmError as exc:
            actions.append(f"merge queue priority for {url} waits: {exc}")
    return actions
