import json
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.ledger_client import LedgerClient, LedgerUnreachable
from scripts.swarm.store import RedisStore, SwarmConfig

TASKS = [
    {"id": "t1", "state": "open", "lane": "eng", "kind": "code", "rank": "high", "depends_on": []},
    {"id": "t2", "state": "claimed", "lane": "ci", "awaiting": "merge", "pr_url": "https://example.test/pull/2"},
]
FREEZES = [{"target": "lane:ci", "verb": "freeze"}, {"target": "tasks/t1", "verb": "focus"}]


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/home/x/dev/agentihooks", 2, 1))
    seen: list[tuple[str, str]] = []

    def tasks(_: LedgerClient, slug: str) -> list[dict[str, Any]]:
        seen.append(("tasks", slug))
        return TASKS if slug == "sw" else []

    def freezes(_: LedgerClient, slug: str) -> list[dict[str, Any]]:
        seen.append(("freezes", slug))
        return FREEZES if slug == "sw" else []

    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(LedgerClient, "tasks", tasks)
    monkeypatch.setattr(LedgerClient, "freezes", freezes)
    return seen


@pytest.mark.feature("swarm-tasks-read-command")
def test_swarm_tasks_prints_every_task_row_and_freeze_of_the_swarm_as_one_json_document(
    reads: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["sw", "tasks"]) == 0
    assert json.loads(capsys.readouterr().out) == {"tasks": TASKS, "freezes": FREEZES}
    assert sorted(reads) == [("freezes", "sw"), ("tasks", "sw")]


@pytest.mark.feature("swarm-tasks-read-command")
def test_swarm_tasks_exits_one_and_prints_no_rows_when_the_ledger_is_unreachable(
    reads: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreachable(_: LedgerClient, slug: str) -> list[dict[str, Any]]:
        raise LedgerUnreachable(f"ledger {slug}: unreachable")

    monkeypatch.setattr(LedgerClient, "tasks", unreachable)
    assert cli.main(["sw", "tasks"]) == 1
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "swarm: ledger sw: unreachable\n")
