"""The one off compaction of the lab records an earlier release stored with test batch manifests in Redis:
python -m scripts.ci_lab compact, run once on the release that keeps them in the object store, before its scheduler
starts. Every manifest a live grade state's slot or a queued, leased or finished job hash carries moves to its unit's
place in the object store and leaves Redis, and an unfinished test slot whose unit has none stored there gets its
batch's slice of the packed dependency manifest its grade keyed on. A grade state the scheduler writes meanwhile stays
as it wrote it."""

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import redis
from redis.exceptions import WatchError

from scripts.ci_lab import contract, expand_tests, queue
from scripts.ci_lab.objects import Objects, manifest_folder
from scripts.ci_lab.scheduler import GRADES, LIVE, UNPACK, keep_manifests, storage
from scripts.ci_lab.units import Unit

__all__ = ["compact", "main"]

FIELD = "manifest"
JOBS = queue.PREFIX + "job:"
PENDING = queue.PREFIX + "pending"
LEASED = queue.PREFIX + "leased"
DONE = queue.PREFIX + "done"

Wanted = defaultdict[tuple[str, str], dict[str, list[str]]]


def compact(client: Any, objects: Objects) -> Counter[str]:
    """Move the manifests every live grade state and every queued, leased or finished job hash carries to the object
    store, slice the ones unfinished test slots lack from their grade's packed manifest and return how many it moved,
    stripped, sliced or left. The live grade set and the queue's state sets name every record it reads, so it runs
    with the lab's Redis user, which may not SCAN."""
    kept: set[str] = set()
    counts: Counter[str] = Counter()
    wanted: Wanted = defaultdict(dict)
    for run in sorted(client.smembers(LIVE)):
        _grade(client, objects, GRADES + run, kept, wanted, counts)
    jobs = {*client.zrange(PENDING, 0, -1), *client.zrange(LEASED, 0, -1), *client.smembers(DONE)}
    for job in sorted(jobs):
        _job(client, objects, JOBS + job, kept, counts)
    for source, batches in wanted.items():
        if missing := {unit: tests for unit, tests in batches.items() if unit not in kept}:
            _slice(objects, source, missing, kept, counts)
    return counts


def _keep(objects: Objects, unit: Unit, kept: set[str], counts: Counter[str], name: str) -> None:
    """Store the unit's manifest under its unit key unless this run already holds it there, counting a write by name."""
    if unit.manifest and unit.key not in kept:
        counts[name] += keep_manifests(objects, [unit])
        kept.add(unit.key)


def _grade(client: Any, objects: Objects, key: str, kept: set[str], wanted: Wanted, counts: Counter[str]) -> None:
    """Move the manifests a grade state's slots carry to the object store and store the state without them unless the
    scheduler wrote it meanwhile, noting each unfinished test slot of a grade keyed on a manifest commit."""
    with client.pipeline() as pipe:
        pipe.watch(key)
        raw = pipe.get(key)
        if raw is None:
            return
        state = json.loads(raw)
        carried = [slot for slot in state["units"].values() if FIELD in slot]
        for slot in carried:
            _keep(objects, Unit(slot["kind"], slot["key"], (), slot.pop(FIELD)), kept, counts, "grade manifests moved")
        commit = state.get("manifest")
        for slot in state["units"].values():
            if commit and slot["kind"] == expand_tests.KIND and slot.get("job") and not slot.get("outcome"):
                wanted[(state["repo"], commit)][slot["key"]] = slot["members"]
        if carried:
            pipe.multi()
            pipe.set(key, json.dumps(state))
            try:
                pipe.execute()
                counts["grades stripped"] += 1
            except WatchError:
                counts["grades written meanwhile"] += 1


def _job(client: Any, objects: Objects, key: str, kept: set[str], counts: Counter[str]) -> None:
    """Move the manifest a job hash carries to its unit's place in the object store and remove it from the hash."""
    kind, unit, text = client.hmget(key, "kind", "key", FIELD)
    if text is not None:
        _keep(objects, Unit(kind, unit, (), text), kept, counts, "job manifests moved")
        client.hdel(key, FIELD)
        counts["jobs stripped"] += 1


def _slice(
    objects: Objects, source: tuple[str, str], batches: Mapping[str, list[str]], kept: set[str], counts: Counter[str]
) -> None:
    """Store each batch's slice of the packed manifest of the repository's commit, or count the batches when that
    commit has none stored."""
    repo, commit = source
    folder = manifest_folder(urlsplit(repo).path.removeprefix("/"))
    path = f"{folder}{commit}.json"
    if path not in objects.keys(folder):
        counts["batches without a packed manifest"] += len(batches)
        return
    records = UNPACK(json.loads(objects.read(path)))["tests"]
    for unit, tests in batches.items():
        text = json.dumps({"tests": {test: records[test] for test in tests if test in records}})
        _keep(objects, Unit(expand_tests.KIND, unit, (), text), kept, counts, "batches sliced")


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Compact the lab records of the Redis the environment names against its object store and print the counts."""
    argparse.ArgumentParser(prog="python -m scripts.ci_lab compact").parse_args(argv)
    client: Any = redis.Redis.from_url(environ[contract.REDIS_URL], decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    print(json.dumps(compact(client, storage(environ))))
    return 0
