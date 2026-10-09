import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import dev_red
from scripts.swarm.store import PREFIX
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.xdist_group("fakeredis")

ENDPOINT = "repos/{owner}/{repo}/actions/workflows/test.yml/runs?branch=dev&event=push&status=completed&per_page=20"
JQ = ".workflow_runs[] | {id, conclusion} | @json"


def gh(runs, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(r) for r in runs), "")

    return run


@pytest.fixture
def swarm():
    import fakeredis

    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    ledger = FakeLedger([{"id": "t1", "state": "blocked", "claimed_by": "engineer@a1b2c3-0001"}])
    ledger.comments = []
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((slug, task, text, by))
    return store, SimpleNamespace(repo="/repo"), ledger


def test_the_record_key_sits_under_the_swarm_prefix():
    assert dev_red.key("sw") == f"{PREFIX}:sw:dev-red"


def test_latest_reads_finished_dev_push_runs_of_tests_and_takes_the_newest():
    calls = []
    runs = [
        {"id": 5, "conclusion": "failure"},
        {"id": 9, "conclusion": "cancelled"},
        {"id": 7, "conclusion": "success"},
    ]
    assert dev_red.latest("/repo", gh(runs, calls)) == {"id": 7, "conclusion": "success"}
    assert calls == [
        (
            ["gh", "api", ENDPOINT, "--jq", JQ],
            {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60},
        )
    ]


def test_latest_is_none_without_a_finished_run():
    assert dev_red.latest("/repo", gh([{"id": 9, "conclusion": "cancelled"}], [])) is None


def test_record_stores_the_failing_dev_run_as_the_block_cause(swarm):
    store, _, _ = swarm
    runs = [{"id": 41, "conclusion": "failure"}, {"id": 40, "conclusion": "success"}]
    assert dev_red.record(store.redis, "sw", "t1", "/repo", gh(runs, [])) == 41
    assert store.redis.hgetall(dev_red.key("sw")) == {"t1": "41"}


@pytest.mark.parametrize("runs", [[], [{"id": 42, "conclusion": "success"}, {"id": 41, "conclusion": "failure"}]])
def test_record_refuses_when_dev_is_not_red(swarm, runs):
    store, _, _ = swarm
    assert dev_red.record(store.redis, "sw", "t1", "/repo", gh(runs, [])) is None
    assert store.redis.hgetall(dev_red.key("sw")) == {}


def test_clear_drops_the_record(swarm):
    store, _, _ = swarm
    store.redis.hset(dev_red.key("sw"), mapping={"t1": "41", "t2": "41"})
    dev_red.clear(store.redis, "sw", "t1")
    assert store.redis.hgetall(dev_red.key("sw")) == {"t2": "41"}


def test_no_record_reads_nothing(swarm):
    store, config, ledger = swarm
    calls = []
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh([], calls)) == []
    assert calls == []


def test_a_later_green_dev_run_reopens_the_task_clears_its_claimant_and_comments(swarm):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")
    runs = [{"id": 42, "conclusion": "success"}]
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(runs, [])) == [
        "task t1 reopened, dev Tests passed after the red run that blocked it"
    ]
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert ledger.comments == [("sw", "t1", dev_red.REOPENED, "swarm")]
    assert (
        dev_red.REOPENED == "Dev Tests passed again after the red run that blocked this task, so the swarm reopened it."
    )
    assert store.redis.hgetall(dev_red.key("sw")) == {}


@pytest.mark.parametrize(
    "runs",
    [
        [{"id": 41, "conclusion": "failure"}],
        [{"id": 43, "conclusion": "failure"}, {"id": 42, "conclusion": "success"}],
        [{"id": 41, "conclusion": "success"}],
        [],
    ],
)
def test_the_task_stays_blocked_until_a_later_dev_run_passes(swarm, runs):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(runs, [])) == []
    assert ledger.rows["t1"]["state"] == "blocked"
    assert ledger.comments == []
    assert store.redis.hgetall(dev_red.key("sw")) == {"t1": "41"}


@pytest.mark.parametrize("state", ["open", "claimed", "done"])
def test_a_task_no_longer_blocked_loses_its_record(swarm, state):
    store, config, ledger = swarm
    ledger.rows["t1"]["state"] = state
    store.redis.hset(dev_red.key("sw"), mapping={"t1": "41", "gone": "41"})
    assert (
        dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh([{"id": 42, "conclusion": "success"}], []))
        == []
    )
    assert ledger.rows["t1"]["state"] == state
    assert ledger.comments == []
    assert store.redis.hgetall(dev_red.key("sw")) == {}


def test_a_lost_reopen_race_leaves_no_comment(swarm):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")
    update = ledger.update_task

    def raced(slug, task_id, fields, by="swarm", if_state=()):
        assert if_state == ("blocked",)
        ledger.rows[task_id]["state"] = "done"
        return update(slug, task_id, fields, by, if_state)

    ledger.update_task = raced
    assert (
        dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh([{"id": 42, "conclusion": "success"}], []))
        == []
    )
    assert ledger.rows["t1"]["state"] == "done"
    assert ledger.comments == []
    assert store.redis.hgetall(dev_red.key("sw")) == {}


def test_a_failed_read_keeps_the_record_and_says_why(swarm, capsys):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")

    def broken(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="gh: offline")

    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, broken) == []
    assert capsys.readouterr().err == "dev red reopen skipped, reading dev Tests runs failed: gh: offline\n"
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.redis.hgetall(dev_red.key("sw")) == {"t1": "41"}
