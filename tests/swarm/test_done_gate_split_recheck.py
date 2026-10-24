import pytest

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from scripts.swarm import done_gate
from scripts.swarm.store import SwarmError
from tests.swarm.test_done_gate import NOW, Ledger, doc, done, pull, store, task  # noqa: F401
from tests.swarm.test_merge_queue import swarm_of

FIRST = "https://github.com/o/r/pull/9"
SECOND = "https://github.com/o/r/pull/10"


def split_done(**fields):
    return task(pr_url=SECOND, split=["1-2", "3-4"], split_pulls=[FIRST, SECOND], **fields)


def reader(seen, states=None):
    def read(url):
        seen.append(url)
        return pull((states or {}).get(url, "MERGED"))

    return read


def test_the_tick_reopens_a_split_task_whose_earlier_half_is_unmerged(store):  # noqa: F811
    seen, ledger = [], Ledger()
    read = reader(seen, {FIRST: "CLOSED"})
    events = [done("t1", "engineer@a1b2c3-0001")]
    actions = done_gate.recheck_pass(store, "sw", doc([split_done()], events), ledger, NOW, read)
    assert actions == ["task t1 reopened, its pull request is closed"]
    assert seen == [FIRST]
    assert ledger.updates == [("t1", {"state": "open", "claimed_by": ""})]
    assert FIRST in ledger.comments[0][1]


def test_the_tick_leaves_a_split_task_done_when_every_half_merged(store):  # noqa: F811
    seen, ledger = [], Ledger()
    read = reader(seen)
    events = [done("t1", "engineer@a1b2c3-0001")]
    assert done_gate.recheck_pass(store, "sw", doc([split_done()], events), ledger, NOW, read) == []
    assert (seen, ledger.updates) == ([FIRST, SECOND], [])
    assert done_gate.recheck_pass(store, "sw", doc([split_done()], events), ledger, NOW, read) == []
    assert seen == [FIRST, SECOND]


def test_a_repeated_bound_pull_request_does_not_stand_for_another_half():
    read = reader([])
    row = {"id": "t1", "kind": "code", "split": ["1-2", "3-4"], "split_pulls": [FIRST, FIRST]}
    assert done_gate.refusal(row, FIRST, read).startswith("half 2 of this split task has no pull request yet")
    assert done_gate._pulls(row) == [FIRST]


def test_pulls_of_an_unsplit_task_is_its_pull_request():
    assert done_gate._pulls({"pr_url": FIRST}) == [FIRST]
    assert done_gate._pulls({"pr_url": FIRST}, SECOND) == [SECOND]
    row = {"pr_url": SECOND, "split": ["1-2", "3-4"], "split_pulls": [FIRST, SECOND, "x"]}
    assert done_gate._pulls(row) == [FIRST, SECOND]


def test_any_bound_half_may_be_merged():
    state = swarm_of("eng")
    rows = [split_done(state="pr")]
    assert done_gate.require_target(state, "sw", "t1", FIRST, rows) is None
    assert done_gate.require_target(state, "sw", "t1", SECOND, rows) is None
    with pytest.raises(SwarmError, match="recorded pull request"):
        done_gate.require_target(state, "sw", "t1", "https://github.com/o/r/pull/11", rows)


def test_a_planned_pass_does_not_stand_for_a_split_half(tmp_path):
    verdicts = Verdicts("sw", intent.NAME, tmp_path)
    verdicts.write("t1", intent.PASS, "planned", NOW, phase="p1", planned="h")
    row = {"id": "t1", "phase": "p1", "split": ["1-2", "3-4"]}
    document = {"tasks": [row]}
    stamped = intent.stamp("sw", "t1", "not a pull request url", document, "enforce", NOW, home=tmp_path)
    assert stamped["verdict"] == intent.PENDING
    assert verdicts.read("t1")["verdict"] == intent.PENDING
    unsplit = {"tasks": [{**row, "split": []}]}
    verdicts.write("t1", intent.PASS, "planned", NOW, phase="p1", planned="h")
    assert intent.stamp("sw", "t1", "x", unsplit, "enforce", NOW, home=tmp_path)["verdict"] == intent.PASS
