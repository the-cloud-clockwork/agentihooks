import fakeredis
import pytest

from scripts.swarm import done_gate
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")

URL = "https://github.com/o/r/pull/9"
DAY_MS = 24 * 60 * 60_000
NOW = 10 * DAY_MS


def pull(state):
    return PullRequest(state, 1 if state == "MERGED" else None, 1, False)


def github(state):
    seen = []

    def read(url):
        seen.append(url)
        return None if state is None else pull(state)

    read.seen = seen
    return read


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_a_code_or_ci_task_closes_only_on_a_merged_pull_request(kind):
    task = {"id": "t1", "kind": kind}
    assert done_gate.refusal(task, URL, github("MERGED")) == ""
    assert done_gate.refusal(task, URL, github("OPEN")) == (
        f"pull request {URL} is open, not merged; merge it, then run swarm done again"
    )
    assert done_gate.refusal(task, URL, github("CLOSED")) == (
        f"pull request {URL} is closed, not merged; merge it, then run swarm done again"
    )


def test_a_task_without_a_kind_is_gated_as_code():
    assert done_gate.refusal({"id": "t1"}, "", github("MERGED")) == (
        "a code task is done only with its merged pull request: give --pr <url>"
    )


def test_an_unreadable_pull_request_is_refused_with_a_retry():
    assert done_gate.refusal({"kind": "ci"}, URL, github(None)) == (
        f"could not read pull request {URL} from GitHub; run swarm done again when it answers"
    )


@pytest.mark.parametrize("kind", ["ops", "tune", "troubleshoot", "research", "plan"])
def test_other_kinds_never_read_github(kind):
    read = github("OPEN")
    assert done_gate.refusal({"kind": kind}, URL, read) == ""
    assert read.seen == []


class Ledger:
    def __init__(self):
        self.updates, self.comments = [], []

    def update_task(self, slug, task_id, fields, by="swarm"):
        self.updates.append((task_id, fields))

    def comment(self, slug, task_id, text, by):
        self.comments.append((task_id, text, by))


def done(task_id, by, at=NOW - 60_000):
    return {"kind": "task done", "target": f"tasks/{task_id}", "by": by, "at": at}


def doc(tasks, events):
    return {"tasks": tasks, "_meta": {"events": events}}


def task(task_id="t1", **fields):
    return {"id": task_id, "state": "done", "kind": "code", "pr_url": URL, **fields}


@pytest.fixture
def store():
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def recheck(store, document, state):
    ledger, read = Ledger(), github(state)
    actions = done_gate.recheck_pass(store, "sw", document, ledger, NOW, read)
    return actions, ledger, read


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_the_tick_reopens_a_task_an_agent_marked_done_before_its_pull_request_merged(store, state):
    document = doc([task()], [done("t1", "engineer@a1b2c3-0001")])
    actions, ledger, _ = recheck(store, document, state)
    word = state.lower()
    assert actions == [f"task t1 reopened, its pull request is {word}"]
    assert ledger.updates == [("t1", {"state": "open", "claimed_by": ""})]
    assert ledger.comments == [
        (
            "t1",
            f"Reopened by the swarm: its pull request {URL} is {word}, not merged. "
            "A code task is done only when its pull request merges.",
            "swarm",
        )
    ]


def test_a_merged_pull_request_is_read_once(store):
    document = doc([task(), task("t2", kind="ci")], [done("t1", "engineer@a1b2c3-0001"), done("t2", "ci@a1b2c3-0001")])
    actions, ledger, read = recheck(store, document, "MERGED")
    assert (actions, ledger.updates, read.seen) == ([], [], [URL])
    actions, ledger, read = recheck(store, document, "OPEN")
    assert (actions, ledger.updates, read.seen) == ([], [], [])


def test_an_unread_pull_request_leaves_the_task_done_and_is_read_again(store):
    document = doc([task()], [done("t1", "engineer@a1b2c3-0001")])
    actions, ledger, read = recheck(store, document, None)
    assert (actions, ledger.updates, read.seen) == ([], [], [URL])
    actions, _, _ = recheck(store, document, "OPEN")
    assert actions == ["task t1 reopened, its pull request is open"]


@pytest.mark.parametrize("by", ["master@a1b2c3-0001", "operator", "swarm", "planner@a1b2c3-0001"])
def test_a_done_marked_outside_the_worker_lanes_stands(store, by):
    actions, ledger, read = recheck(store, doc([task()], [done("t1", by)]), "OPEN")
    assert (actions, ledger.updates, read.seen) == ([], [], [])


def test_only_the_latest_done_counts(store):
    events = [done("t1", "engineer@a1b2c3-0001", NOW - 120_000), done("t1", "master@a1b2c3-0001")]
    actions, _, read = recheck(store, doc([task()], events), "OPEN")
    assert (actions, read.seen) == ([], [])
    events = [done("t1", "master@a1b2c3-0001", NOW - 120_000), done("t1", "engineer@a1b2c3-0001")]
    actions, _, _ = recheck(store, doc([task()], events), "OPEN")
    assert actions == ["task t1 reopened, its pull request is open"]


def test_a_done_older_than_a_day_is_not_read(store):
    old = done("t1", "engineer@a1b2c3-0001", NOW - DAY_MS - 1)
    actions, _, read = recheck(store, doc([task()], [old]), "OPEN")
    assert (actions, read.seen) == ([], [])
    edge = done("t1", "engineer@a1b2c3-0001", NOW - DAY_MS)
    actions, _, _ = recheck(store, doc([task()], [edge]), "OPEN")
    assert actions == ["task t1 reopened, its pull request is open"]


@pytest.mark.parametrize(
    "row",
    [
        task(state="open"),
        task(state="pr"),
        task(kind="ops"),
        task(kind="research"),
        task(pr_url=""),
        {"id": "t1", "state": "done", "kind": "code"},
    ],
)
def test_only_done_code_and_ci_tasks_with_a_pull_request_are_read(store, row):
    actions, _, read = recheck(store, doc([row], [done("t1", "engineer@a1b2c3-0001")]), "OPEN")
    assert (actions, read.seen) == ([], [])


def test_events_for_missing_tasks_or_other_targets_are_skipped(store):
    events = [
        done("gone", "engineer@a1b2c3-0001"),
        {"kind": "task done", "target": "phases/p1", "by": "engineer@a1b2c3-0001", "at": NOW},
        {"kind": "task pr", "target": "tasks/t1", "by": "engineer@a1b2c3-0001", "at": NOW},
    ]
    actions, _, read = recheck(store, doc([task()], events), "OPEN")
    assert (actions, read.seen) == ([], [])


def test_a_ledger_without_events_reads_nothing(store):
    actions, _, read = recheck(store, {"tasks": [task()]}, "OPEN")
    assert (actions, read.seen) == ([], [])
