import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import dev_red
from scripts.swarm.store import PREFIX, RedisStore
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.xdist_group("fakeredis")

ENDPOINT = "repos/{owner}/{repo}/actions/workflows/test.yml/runs?branch=dev&event=push&status=completed&per_page=20"
JQ = ".workflow_runs[] | {id, conclusion} | @json"
GREEN = [{"id": 42, "conclusion": "success"}]


def gh(runs, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(r) for r in runs), "")

    return run


@pytest.fixture
def swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
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


def test_failing_run_names_the_red_dev_run():
    runs = [{"id": 41, "conclusion": "failure"}, {"id": 40, "conclusion": "success"}]
    assert dev_red.failing_run("/repo", gh(runs, [])) == 41


@pytest.mark.parametrize("runs", [[], [{"id": 42, "conclusion": "success"}, {"id": 41, "conclusion": "failure"}]])
def test_failing_run_is_none_when_dev_is_not_red(runs):
    assert dev_red.failing_run("/repo", gh(runs, [])) is None


def test_hold_records_a_run_and_clears_on_none(swarm):
    store, _, _ = swarm
    dev_red.hold(store.redis, "sw", "t1", 41)
    dev_red.hold(store.redis, "sw", "t2", 41)
    assert store.redis.hgetall(dev_red.key("sw")) == {"t1": "41", "t2": "41"}
    dev_red.hold(store.redis, "sw", "t1", None)
    assert store.redis.hgetall(dev_red.key("sw")) == {"t2": "41"}


def test_no_record_reads_nothing(swarm):
    store, config, ledger = swarm
    calls = []
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(GREEN, calls)) == []
    assert calls == []


@pytest.mark.parametrize("runs", [GREEN, [{"id": 41, "conclusion": "success"}]])
def test_a_green_dev_run_at_or_after_the_red_one_reopens_the_task_and_comments(swarm, runs):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")
    store.redis.hset(store.key("sw", "started-lives"), "t1", 3)
    calls = []
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(runs, calls)) == [
        "task t1 reopened, dev Tests passed after the red run that blocked it"
    ]
    assert [kwargs["cwd"] for _, kwargs in calls] == ["/repo"]
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert ledger.comments == [("sw", "t1", dev_red.REOPENED, "swarm")]
    assert (
        dev_red.REOPENED == "Dev Tests passed again after the red run that blocked this task, so the swarm reopened it."
    )
    assert store.redis.hgetall(dev_red.key("sw")) == {}
    assert store.claims("sw", "t1") == 2


@pytest.mark.parametrize(
    "runs",
    [
        [{"id": 41, "conclusion": "failure"}],
        [{"id": 43, "conclusion": "failure"}, {"id": 42, "conclusion": "success"}],
        [{"id": 40, "conclusion": "success"}],
        [],
    ],
)
def test_the_task_stays_blocked_while_dev_is_red(swarm, runs):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(runs, [])) == []
    assert ledger.rows["t1"]["state"] == "blocked"
    assert ledger.comments == []
    assert store.redis.hgetall(dev_red.key("sw")) == {"t1": "41"}


@pytest.mark.parametrize("runs", [[{"id": 2, "conclusion": "failure"}], []])
def test_no_green_run_reopens_even_the_lowest_red_run(swarm, runs):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "1")
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(runs, [])) == []
    assert ledger.rows["t1"]["state"] == "blocked"


@pytest.mark.parametrize("state", ["open", "claimed", "done"])
def test_a_task_no_longer_blocked_loses_its_record_without_a_read(swarm, state):
    store, config, ledger = swarm
    ledger.rows["t1"]["state"] = state
    store.redis.hset(dev_red.key("sw"), mapping={"t1": "41", "gone": "41"})
    calls = []
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(GREEN, calls)) == []
    assert calls == []
    assert ledger.rows["t1"]["state"] == state
    assert ledger.comments == []
    assert store.redis.hgetall(dev_red.key("sw")) == {}


def test_a_lost_reopen_race_leaves_no_comment_and_no_refund(swarm):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")
    store.redis.hset(store.key("sw", "started-lives"), "t1", 3)
    update = ledger.update_task

    def raced(slug, task_id, fields, by="swarm", if_state=()):
        assert if_state == ("blocked",)
        ledger.rows[task_id]["state"] = "done"
        return update(slug, task_id, fields, by, if_state)

    ledger.update_task = raced
    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, gh(GREEN, [])) == []
    assert ledger.rows["t1"]["state"] == "done"
    assert ledger.comments == []
    assert store.redis.hgetall(dev_red.key("sw")) == {}
    assert store.claims("sw", "t1") == 3


@pytest.mark.parametrize(
    "error, said",
    [
        (subprocess.CalledProcessError(1, ["gh"], stderr="gh: offline"), "gh: offline"),
        (FileNotFoundError("no gh"), "no gh"),
        (ValueError("bad json"), "bad json"),
        (KeyError("conclusion"), "'conclusion'"),
    ],
)
def test_a_failed_read_keeps_the_record_and_says_why(swarm, capsys, error, said):
    store, config, ledger = swarm
    store.redis.hset(dev_red.key("sw"), "t1", "41")

    def broken(argv, **kwargs):
        raise error

    assert dev_red.reopen_pass("sw", config, store, ledger, ledger.rows, broken) == []
    assert capsys.readouterr().err == f"dev red reopen skipped, reading dev Tests runs failed: {said}\n"
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.redis.hgetall(dev_red.key("sw")) == {"t1": "41"}
