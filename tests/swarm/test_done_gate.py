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
    task, read = {"id": "t1", "kind": kind}, github("MERGED")
    assert done_gate.refusal(task, URL, read) == ""
    assert read.seen == [URL]
    assert done_gate.refusal(task, URL, github("OPEN")) == (
        f"pull request {URL} is open, not merged; merge it, then run swarm done again"
    )
    assert done_gate.refusal(task, URL, github("CLOSED")) == (
        f"pull request {URL} is closed, not merged; merge it, then run swarm done again"
    )


def test_distributed_outcomes_cannot_use_the_legacy_final_mutation_path(monkeypatch):
    from scripts.swarm.store import SwarmError
    from tests.sv2_ctl02_cases import build

    state, authority, controller, clock, start = build(monkeypatch)
    assert done_gate.require_local(state, authority.slug, "task") is None
    _, token = start()
    authority.admit(token, 30_000)
    with pytest.raises(SwarmError, match="controller outcome"):
        done_gate.require_local(state, authority.slug, "task")


@pytest.mark.parametrize("record", ["task-authority", "claim-journal"])
def test_either_distributed_record_alone_blocks_legacy_completion(record):
    from scripts.swarm.store import SwarmError
    from tests.swarm.test_merge_queue import swarm_of

    state = swarm_of("eng")
    state.redis.set(state.key("sw", record, "t1"), "{}")
    with pytest.raises(SwarmError, match="^distributed final mutations require the controller outcome path$"):
        done_gate.require_local(state, "sw", "t1")


def test_merge_target_requires_the_recorded_task_and_checks_shared_links():
    from scripts.swarm.store import SwarmError
    from tests.swarm.test_merge_queue import swarm_of

    state = swarm_of("eng")
    rows = [{"id": "t1", "pr_url": URL}, {"id": "t2", "pr_url": URL}]
    assert done_gate.require_target(state, "sw", "t1", URL, rows) is None
    with pytest.raises(SwarmError, match="^final integration must target this task's recorded pull request$"):
        done_gate.require_target(state, "sw", "missing", URL, rows)
    state.redis.set(state.key("sw", "task-authority", "t2"), "{}")
    with pytest.raises(SwarmError, match="controller outcome"):
        done_gate.require_target(state, "sw", "t1", URL, rows)


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
        assert (slug, by) == ("sw", "swarm")
        self.updates.append((task_id, fields))

    def comment(self, slug, task_id, text, by):
        assert slug == "sw"
        self.comments.append((task_id, text, by))


def done(task_id, by, at=NOW - 60_000):
    return {"kind": "task done", "target": f"tasks/{task_id}", "by": by, "at": at}


def doc(tasks, events):
    return {"tasks": tasks, "_meta": {"events": events}}


def task(task_id="t1", **fields):
    return {"id": task_id, "state": "done", "kind": "code", "pr_url": URL, **fields}


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def recheck(store, document, state, slug="sw"):
    ledger, read = Ledger(), github(state)
    actions = done_gate.recheck_pass(store, slug, document, ledger, NOW, read)
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
    seen = store.key("sw", "done-merged", URL)
    assert (store.redis.get(seen), store.redis.ttl(seen)) == ("1", done_gate.SEEN_TTL_S)
    actions, ledger, read = recheck(store, document, "OPEN")
    assert (actions, ledger.updates, read.seen) == ([], [], [])


def test_the_merged_mark_belongs_to_one_swarm_and_one_pull_request(store):
    other = "https://github.com/o/r/pull/10"
    store.redis.set(store.key("other", "done-merged", URL), 1)
    store.redis.set(store.key("sw", "done-merged", other), 1)
    actions, _, read = recheck(store, doc([task()], [done("t1", "engineer@a1b2c3-0001")]), "OPEN")
    assert (actions, read.seen) == (["task t1 reopened, its pull request is open"], [URL])


@pytest.mark.parametrize("skipped", ["merged", "unread", "manual"])
def test_one_skipped_task_never_stops_the_pass(store, skipped):
    first = task("t1", pr_url="https://github.com/o/r/pull/1")
    if skipped == "manual":
        first["state"] = "open"
    if skipped == "merged":
        store.redis.set(store.key("sw", "done-merged", first["pr_url"]), 1)
    events = [done("t1", "engineer@a1b2c3-0001"), done("t2", "engineer@a1b2c3-0002")]
    ledger, read = Ledger(), (lambda url: None if skipped == "unread" and url == first["pr_url"] else pull("OPEN"))
    actions = done_gate.recheck_pass(store, "sw", doc([first, task("t2")], events), ledger, NOW, read)
    assert actions == ["task t2 reopened, its pull request is open"]


def test_a_merged_task_never_stops_the_pass(store):
    first = task("t1", pr_url="https://github.com/o/r/pull/1")
    events = [done("t1", "engineer@a1b2c3-0001"), done("t2", "engineer@a1b2c3-0002")]
    ledger, read = Ledger(), (lambda url: pull("MERGED" if url == first["pr_url"] else "CLOSED"))
    actions = done_gate.recheck_pass(store, "sw", doc([first, task("t2")], events), ledger, NOW, read)
    assert actions == ["task t2 reopened, its pull request is closed"]


def test_a_ledger_without_tasks_reads_nothing(store):
    actions, _, read = recheck(store, {"_meta": {"events": [done("t1", "engineer@a1b2c3-0001")]}}, "OPEN")
    assert (actions, read.seen) == ([], [])


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
