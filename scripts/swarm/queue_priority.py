import json
import subprocess

from scripts.inbox.store import InboxStore
from scripts.swarm import merge_queue, queue_record
from scripts.swarm.store import RedisStore, SwarmError

PENDING = "queue-priority"


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
    store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], journal: queue_record.Journal
) -> dict:
    result = merge_queue.prioritize(url, journal=journal)
    for task in tasks:
        if task.get("pr_url") in result["displaced"] and task.get("claimed_by"):
            InboxStore(store.redis).send(
                sender,
                task["claimed_by"],
                f"Your pull request {task['pr_url']} was requeued in its original order after priority request {url}.",
            )
    if result["errors"]:
        raise SwarmError("; ".join(result["errors"]))
    store.redis.hdel(store.key(slug, PENDING), url)
    return result


def request(store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
    journal = queue_record.Journal(url)
    held = None if lift else blocking(url, journal)
    if held is not None:
        record = {**held, "sender": sender}
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
    return ""


def refresh(store: RedisStore, slug: str, tasks: list[dict]) -> list[str]:
    actions = []
    for url, raw in store.redis.hgetall(store.key(slug, PENDING)).items():
        record = json.loads(raw)
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
                    store.redis.hset(store.key(slug, PENDING), url, json.dumps({**held, "sender": record["sender"]}))
                    continue
            else:
                run = merge_queue.rest(f"repos/{record['repo']}/actions/runs/{record['run_id']}", subprocess.run)
                if run["status"] != "completed":
                    continue
            apply(store, slug, url, record["sender"], tasks, journal)
            journal.write("priority-applied", url, "", None, None, **record)
            actions.append(f"applied deferred merge queue priority for {url}")
        except SwarmError as exc:
            actions.append(f"merge queue priority for {url} waits: {exc}")
    return actions
