"""A scheduled workflow placed on a hive sends the text of its case file in the run order, never the file location."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytestmark = pytest.mark.unit

NOW = 1_794_000_000_000
HIVE = "hive-a"
RUN = "entry-1:slot-1"
OWNER = "me"
CASE = '{"goal": "grade the nightly build"}\n'


@pytest.fixture
def redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.mark.feature("hive-run-case-text")
def test_a_hive_placed_scheduled_run_sends_its_case_file_text(
    redis: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.events.redis_state import RedisStateStore
    from scripts.hive import registry, run_orders, run_placement
    from scripts.scheduler.commands import target_slot

    repo = tmp_path / "repo"
    repo.mkdir()
    case = tmp_path / "case.json"
    case.write_text(CASE)
    monkeypatch.chdir(repo)
    hive = {
        "id": HIVE,
        "repos": [str(repo)],
        "capabilities": ["build"],
        "heartbeat_at": NOW,
        "beat_at": NOW,
        "draining": "no",
    }
    monkeypatch.setattr(run_placement, "time", SimpleNamespace(time=lambda: NOW / 1000))
    monkeypatch.setattr(registry, "hives", lambda client: [hive] if client is redis else [])
    slot = target_slot("workflow", "nightly", case=str(case), placement="hive", needs="build")

    slot.deliver({"owner": OWNER}, RUN, RedisStateStore(redis, "scheduler"))

    [(_, fields)] = cast("list[tuple[str, dict[str, str]]]", redis.xrange(run_orders.RunOrders(redis).stream(HIVE)))
    assert run_orders.RunOrder.decode(fields["order"]).case == CASE
