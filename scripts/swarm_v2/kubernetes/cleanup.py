"""Removes a final attempt's Pods and Services by pinned UID, then releases its attach registration and secret references."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes.client import DELETABLE, PreconditionFailed
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL, owner_for

KINDS = DELETABLE
STEPS = (*KINDS, "attach", "secrets")


class CleanupApi(Protocol):
    namespace: str

    def list_pods(self, selector: str) -> list[dict]: ...

    def list_services(self, selector: str) -> list[dict]: ...

    def delete(self, kind: str, name: str, uid: str) -> bool: ...


class Release(Protocol):
    def release(self, execution_id: str) -> None:
        """Idempotent: releasing an already released execution is not an error."""
        ...


@dataclass(frozen=True)
class Result:
    execution_id: str
    state: str
    removed: dict = field(default_factory=dict)


class Cleanup:
    """`require` must raise unless this process holds the current controller lease and a scoped grant."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        api: CleanupApi,
        require: Callable[[], None],
        attach: Release,
        secrets: Release,
        enabled: bool = True,
    ) -> None:
        self.store, self.slug, self.api, self.require = store, slug, api, require
        self.releases = {"attach": attach, "secrets": secrets}
        self.enabled, self.owner = enabled, owner_for(slug)
        self.journal_key = store.key(slug, "cleanup-journal")

    def run(self, execution_id: str) -> Result:
        if not self.enabled:
            return Result(execution_id, "suspended")
        self.require()
        entry = retention.final(self.store, self.slug, execution_id)
        if entry is None:
            return Result(execution_id, "not_final")
        kept = retention.retained(self.store, self.slug, execution_id)
        if kept:
            self.store.redis.hdel(self.journal_key, execution_id)
            return Result(execution_id, "done", kept["removed"])
        wait = retention.waiting(self.store, self.slug, entry)
        if wait:
            return Result(execution_id, wait)
        raw = self.store.redis.hget(self.journal_key, execution_id)
        journal = json.loads(raw) if raw else self._pin(entry)
        for step in STEPS:
            if step not in journal["done"]:
                self._step(step, journal)
                journal["done"].append(step)
                self._save(journal)
        self.require()
        retention.retain(self.store, self.slug, entry, journal["removed"])
        self.store.redis.hdel(self.journal_key, execution_id)
        return Result(execution_id, "done", journal["removed"])

    def _pin(self, entry: retention.Final) -> dict:
        selector = self._selector(entry.execution_id, entry.generation)
        found = {"pods": self.api.list_pods(selector), "services": self.api.list_services(selector)}
        journal = {
            "execution_id": entry.execution_id,
            "generation": entry.generation,
            "pinned": {
                kind: [{"name": item["metadata"]["name"], "uid": item["metadata"]["uid"]} for item in found[kind]]
                for kind in KINDS
            },
            "sent": [],
            "done": [],
            "removed": {kind: [] for kind in KINDS},
        }
        self._save(journal)
        return journal

    def _step(self, step: str, journal: dict) -> None:
        if step in self.releases:
            self.require()
            self.releases[step].release(journal["execution_id"])
            return
        listing = self.api.list_pods if step == "pods" else self.api.list_services
        live = {
            item["metadata"]["uid"] for item in listing(self._selector(journal["execution_id"], journal["generation"]))
        }
        settled = {item["uid"] for item in journal["removed"][step]}
        for pinned in journal["pinned"][step]:
            if pinned["uid"] not in settled:
                journal["removed"][step].append({**pinned, "outcome": self._remove(step, pinned, live, journal)})
                self._save(journal)

    def _remove(self, kind: str, pinned: dict, live: set, journal: dict) -> str:
        resent = pinned["uid"] in journal["sent"]
        gone = "absent_after_send" if resent else "absent"
        if pinned["uid"] not in live:
            return gone
        if not resent:
            journal["sent"].append(pinned["uid"])
            self._save(journal)
        self.require()
        try:
            return "deleted" if self.api.delete(kind, pinned["name"], pinned["uid"]) else gone
        except PreconditionFailed:
            return "replaced"

    def _selector(self, execution_id: str, generation: int) -> str:
        return f"{OWNER_LABEL}={self.owner},{EXECUTION_LABEL}={execution_id},{GENERATION_LABEL}={generation}"

    def _save(self, journal: dict) -> None:
        self.require()
        self.store.redis.hset(self.journal_key, journal["execution_id"], json.dumps(journal))
