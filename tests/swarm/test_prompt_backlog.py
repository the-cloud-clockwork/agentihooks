import json
from pathlib import Path

import pytest

from scripts.swarm import cli, status
from scripts.swarm.health import prompt_backlog
from scripts.swarm.health.findings import Finding
from scripts.swarm_v2.herdr.target import DELIVERIES
from tests.swarm.test_cli import env as env
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_spawn_stall import stalled as stalled

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    import fakeredis

    from scripts.swarm.store import RedisStore

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def journal(path, pending, since=1000):
    entries = {f"op-{n}": {"target": "exe-a/2/w1:p1", "state": "pending", "since": since} for n in range(pending)}
    entries["op-done"] = {"target": "exe-a/2/w1:p1", "state": "refused", "reason": "herdr exited 1"}
    path.write_text(json.dumps({"version": 1, "deliveries": entries}))


def observed(store, path, counts):
    for count in counts:
        journal(path, count)
        prompt_backlog.observe(store, "sw", 1000, path)
    return prompt_backlog.findings(store, "sw")


def controller_home():
    folder = Path.home() / ".ssh" / "swarm-terminals"
    folder.mkdir(parents=True)
    return folder / DELIVERIES


def test_pending_prompts_rising_three_times_in_a_row_raise_a_finding(store, tmp_path):
    assert observed(store, tmp_path / DELIVERIES, [0, 1, 1, 2, 3]) == [
        Finding(
            "prompt backlog",
            "sw",
            "Remote prompts awaiting an outcome keep growing",
            ("Pending prompts rose 0 to 1 to 2 to 3",),
            "three rises in a row",
            3,
        )
    ]
    assert prompt_backlog.read(store, "sw") == [0, 1, 2, 3]


@pytest.mark.parametrize("counts", [[1, 2, 3], [0, 1, 2, 3, 0], [0, 1, 3, 2], [5, 5, 5, 5], [3, 2, 1, 0]])
def test_pending_prompts_that_stop_growing_raise_nothing(store, tmp_path, counts):
    assert observed(store, tmp_path / DELIVERIES, counts) == []


def test_a_repeated_sample_breaks_the_rise(store):
    store.redis.set(store.key("sw", "prompt-backlog"), json.dumps([0, 1, 1, 2]))
    assert prompt_backlog.findings(store, "sw") == []


def test_the_samples_keep_only_the_last_rises(store, tmp_path):
    observed(store, tmp_path / DELIVERIES, [0, 1, 2, 3, 4, 5])
    assert prompt_backlog.read(store, "sw") == [2, 3, 4, 5]


def test_an_unreadable_journal_keeps_the_samples(store, tmp_path):
    path = tmp_path / DELIVERIES
    observed(store, path, [0, 1, 2])
    path.write_text("{")
    assert prompt_backlog.observe(store, "sw", 1000, path) == [0, 1, 2]
    assert observed(store, path, [3])[0].measure == 3


def test_a_missing_journal_counts_no_pending_prompt(store, tmp_path):
    assert prompt_backlog.observe(store, "sw", 1000, tmp_path / DELIVERIES) == [0]


def test_prompts_pending_past_the_window_are_not_counted(store, tmp_path):
    journal(tmp_path / DELIVERIES, 2)
    assert prompt_backlog.observe(store, "sw", 1599, tmp_path / DELIVERIES) == [2]
    assert prompt_backlog.observe(store, "sw", 1600, tmp_path / DELIVERIES) == [2, 0]


def test_the_controller_journal_is_read_by_default(store):
    path = controller_home()
    for count in [0, 1, 2, 3]:
        journal(path, count)
        prompt_backlog.observe(store, "sw", 1000)
    assert prompt_backlog.findings(store, "sw")[0].measure == 3


def test_the_tick_samples_the_controller_journal(stalled):
    store, ledger, runtime, clock, error = stalled
    journal(controller_home(), 2, since=clock[0] // 1000)
    with pytest.raises(RuntimeError) as caught:
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert caught.value is error
    assert prompt_backlog.read(store, "sw") == [2]


def test_swarm_status_shows_the_backlog_finding(stalled, tmp_path):
    store, ledger, _, clock, _ = stalled
    observed(store, tmp_path / DELIVERIES, [0, 1, 2, 3])
    rows = status.findings(store, "sw", store.config("sw"), ledger.tasks("sw"), [])
    (found,) = [row for row in rows if row["kind"] == "prompt backlog"]
    assert found == {
        "kind": "prompt backlog",
        "subject": "sw",
        "summary": "Remote prompts awaiting an outcome keep growing",
        "evidence": ["Pending prompts rose 0 to 1 to 2 to 3"],
        "threshold": "three rises in a row",
        "id": "prompt-backlog/sw",
        "verdict": None,
        "seen_at": clock[0],
    }
