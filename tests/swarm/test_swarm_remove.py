"""Removing a swarm deletes the task copies every hive keeps of it and leaves every other swarm's copies."""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_workspace

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-remove-deletes-hive-task-copies")
def test_remove_deletes_the_swarm_task_copies_on_every_hive_by_scanning_pages_and_keeps_other_swarms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)
    for slug in ("sw", "swx"):
        store.create(SwarmConfig(slug, "/repo", 1, 0))
    copies = {
        (hive, slug): ledger_workspace.task_files_key(hive, slug, "t1")
        for hive in ("h1", "h2")
        for slug in ("sw", "swx")
    }
    for key in copies.values():
        redis.set(key, json.dumps({"progress.md": "step\n"}))
    redis.set(ledger_workspace.files_key("h1"), json.dumps({"sw": {}}))
    ledger_workspace.launched_on(store, "sw", "t1", "h1")
    page = 2
    monkeypatch.setattr("scripts.swarm.store.SCAN_PAGE", page)
    scans: list[tuple[str | None, int | None]] = []
    deleted: list[int] = []
    scan, delete = redis.scan_iter, redis.delete

    def paged(match: str | None = None, count: int | None = None, **kwargs: Any) -> Any:
        scans.append((match, count))
        return scan(match=match, count=count, **kwargs)

    def chunked(*names: str) -> int:
        deleted.append(len(names))
        return delete(*names)

    def listed(*args: Any, **kwargs: Any) -> list[str]:
        raise AssertionError("removing a swarm scans in pages and never lists keys")

    monkeypatch.setattr(redis, "scan_iter", paged)
    monkeypatch.setattr(redis, "delete", chunked)
    monkeypatch.setattr(redis, "keys", listed)
    assert cli.main(["sw", "remove"]) == 0
    assert [key for key in copies.values() if redis.exists(key)] == [copies["h1", "swx"], copies["h2", "swx"]]
    assert redis.get(store.key("sw", ledger_workspace.WORK_FOLDER, "t1")) is None
    assert redis.exists(ledger_workspace.files_key("h1"))
    assert {(store.key("sw", "*"), page), (ledger_workspace.task_files_key("*", "sw", "*"), page)} <= set(scans)
    assert max(deleted) == page
    assert store.slugs() == ["swx"]
