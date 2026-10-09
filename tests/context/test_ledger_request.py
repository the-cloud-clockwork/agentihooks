import pytest

from hooks.context import conditions, ledger_request, operator_words
from scripts.swarm_ledger.repository.sqlite import DATABASE
from tests.swarm_ledger import legacy_page

pytestmark = pytest.mark.unit

NOW = 10_000.0
REQUEST = "set the no code edits conditions for master, planner and qa"
MASTER = "master@a1-1"


@pytest.fixture
def ledger(tmp_path):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    env = {"LEDGER_DIR": str(folder), "AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_SWARM_TASK": "t1"}

    def write(comments, other=(), **task):
        members = {MASTER: {"role": "orchestrator"}, "eng-1@demo": {"role": "member"}}
        doc = {
            "tasks": [{"id": "t0", "comments": list(other)}, {"id": "t1", "comments": comments, **task}],
            "_meta": {"members": members},
        }
        legacy_page.store(folder, "demo", doc)
        return env

    return write


def comment(cid, text, age=60, by="operator", **marks):
    return {"id": cid, "by": by, "at": int((NOW - age) * 1000), "text": text, **marks}


def relay(cid, quote, by=MASTER, text="The operator asks for this on your task"):
    return comment(cid, text, relayed_by=by, relayed_from="master pane", quote=quote)


def find(env):
    return ledger_request.find(conditions.contains_condition_signal, env)


def test_the_newest_operator_comment_asking_on_the_agents_own_task_opens_it(ledger):
    env = ledger([comment("c-1", REQUEST, age=120), comment("c-2", REQUEST), comment("c-3", "looks good")])
    assert find(env) == ("ledger", "c-2")


def test_an_operator_comment_that_asks_for_nothing_opens_nothing(ledger):
    assert find(ledger([comment("c-1", "looks good, carry on")])) is None


def test_agent_comments_and_other_tasks_never_open_it(ledger):
    assert find(ledger([comment("c-1", REQUEST, by=MASTER)], other=[comment("c-0", REQUEST)])) is None


def test_later_agent_and_deleted_comments_leave_an_earlier_request_standing(ledger):
    later = [comment("c-2", "on it", by=MASTER), {**comment("c-3", "x"), "deleted": True}]
    assert find(ledger([comment("c-1", REQUEST, age=120), *later])) == ("ledger", "c-1")


def test_a_deleted_operator_comment_never_opens_it(ledger):
    assert find(ledger([{**comment("c-1", REQUEST), "deleted": True}])) is None


def test_an_operator_comment_older_than_any_window_holds_while_its_task_is_open(ledger):
    assert find(ledger([comment("c-1", REQUEST, age=7 * 24 * 3600)], state="claimed")) == ("ledger", "c-1")


def test_an_old_approval_on_another_task_never_opens_it(ledger):
    assert find(ledger([], other=[comment("c-0", REQUEST, age=7200)])) is None


@pytest.mark.parametrize("closed", [{"state": "done", "done": True}, {"out_of_scope": True}, {"deleted": True}])
def test_an_approval_closes_when_its_task_is_done_or_cancelled(ledger, closed):
    operator_words.record(MASTER, REQUEST, now=NOW - 60)
    assert find(ledger([comment("c-1", REQUEST)], **closed)) is None
    assert find(ledger([relay("c-2", REQUEST)], **closed)) is None


def test_a_master_relay_opens_it_on_the_operators_words_the_ledger_stored(ledger):
    assert find(ledger([relay("c-1", f"Approve the broker merge. {REQUEST}")])) == ("relay", "c-1")


def test_a_master_relay_holds_after_the_master_session_lost_the_typed_words(ledger):
    operator_words.record(MASTER, REQUEST, now=NOW - 7200)
    old = {**relay("c-1", REQUEST), "at": int((NOW - 7200) * 1000)}
    assert find(ledger([old])) == ("relay", "c-1")


def test_the_relay_text_alone_never_opens_it(ledger):
    operator_words.record(MASTER, "approve the broker merge", now=NOW - 60)
    assert find(ledger([relay("c-1", "approve the broker merge", text=REQUEST)])) is None


def test_a_relay_by_an_agent_that_is_not_the_master_stays_refused(ledger):
    operator_words.record("eng-1@demo", REQUEST, now=NOW - 60)
    assert find(ledger([relay("c-1", REQUEST, by="eng-1@demo")])) is None


def test_a_session_without_its_swarm_task_or_ledger_finds_nothing(ledger, tmp_path):
    env = ledger([comment("c-1", REQUEST)])
    assert find({**env, "AGENTIHOOKS_SWARM_TASK": "t9"}) is None
    assert find({k: v for k, v in env.items() if k != "AGENTIHOOKS_SWARM_TASK"}) is None
    assert find({**env, "AGENTIHOOKS_SWARM": ""}) is None
    assert find({**env, "AGENTIHOOKS_SWARM": "other"}) is None
    (tmp_path / "ledgers" / DATABASE).unlink()
    assert find(env) is None


def test_the_ledger_folder_defaults_to_the_home_development_ledger(tmp_path, monkeypatch):
    folder = tmp_path / "home" / "development-ledger"
    folder.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    tasks = [{"id": "t1", "comments": [comment("c-1", REQUEST)]}]
    legacy_page.store(folder, "demo", {"tasks": tasks, "_meta": {"members": {}}})
    env = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_SWARM_TASK": "t1"}
    assert find(env) == ("ledger", "c-1")


def test_the_ledger_read_takes_members_and_the_bound_task_or_every_task(ledger):
    env = ledger([])
    members = {"_meta": {"members": {MASTER: {"role": "orchestrator"}, "eng-1@demo": {"role": "member"}}}}
    assert ledger_request._ledger(env) == dict(members, tasks=[{"id": "t1", "comments": []}])
    unbound = {key: value for key, value in env.items() if key != "AGENTIHOOKS_SWARM_TASK"}
    assert ledger_request._ledger(unbound) == dict(
        members, tasks=[{"id": "t0", "comments": []}, {"id": "t1", "comments": []}]
    )
