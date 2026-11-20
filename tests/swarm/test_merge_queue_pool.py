import subprocess
from collections.abc import Callable
from queue import Queue
from threading import Barrier, BoundedSemaphore, Thread
from typing import Any

import pytest

from scripts.swarm import merge_queue
from scripts.tick import pool
from scripts.tick.adapters.github import GitHubSourceControl


@pytest.mark.feature("merge-queue-single-github-slot")
def test_four_graphql_calls_complete_with_the_real_github_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", "user")
    monkeypatch.setitem(pool._GATES, "github", BoundedSemaphore(pool.LIMITS["github"]))
    barrier = Barrier(4)
    original_port = merge_queue._port
    answers: Queue[dict[str, Any]] = Queue()
    calls: Queue[list[str]] = Queue()

    def port(run: Callable[..., Any]) -> GitHubSourceControl:
        barrier.wait(timeout=5)
        return original_port(run)

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.put(command)
        return subprocess.CompletedProcess(command, 0, '{"data": {"resource": {"number": 42}}}', "")

    def read() -> None:
        answers.put(merge_queue.graphql(merge_queue.STATE, {"url": "https://github.com/o/r/pull/42"}, run))

    monkeypatch.setattr(merge_queue, "_port", port)
    threads = [Thread(target=read, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for _ in threads:
        assert answers.get(timeout=10) == {"resource": {"number": 42}}
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert calls.qsize() == 4
