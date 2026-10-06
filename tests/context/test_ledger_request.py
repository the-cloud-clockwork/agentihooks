import json

import pytest

from hooks.context import conditions, ledger_request, operator_words

pytestmark = pytest.mark.unit

NOW = 10_000.0
REQUEST = "set the no code edits conditions for master, planner and qa"
MASTER = "master@a1-1"


@pytest.fixture
def ledger(tmp_path):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    env = {"LEDGER_DIR": str(folder), "AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_SWARM_TASK": "t1"}

    def write(comments, other=()):
        members = {MASTER: {"role": "orchestrator"}, "eng-1@demo": {"role": "member"}}
        doc = {
            "tasks": [{"id": "t0", "comments": list(other)}, {"id": "t1", "comments": comments}],
            "_meta": {"members": members},
        }
        (folder / "demo.json").write_text(json.dumps(doc))
        return env

    return write


def comment(cid, text, age=60, by="operator", **marks):
    return {"id": cid, "by": by, "at": int((NOW - age) * 1000), "text": text, **marks}


def relay(cid, quote, by=MASTER, text="The operator asks for this on your task"):
    return comment(cid, text, relayed_by=by, relayed_from="master pane", quote=quote)


def find(env):
    return ledger_request.find(conditions.contains_condition_signal, env, NOW)


def test_the_newest_operator_comment_asking_on_the_agents_own_task_opens_it(ledger):
    env = ledger([comment("c-1", REQUEST, age=120), comment("c-2", REQUEST), comment("c-3", "looks good")])
    assert find(env) == ("ledger", "c-2")


def test_an_operator_comment_that_asks_for_nothing_opens_nothing(ledger):
    assert find(ledger([comment("c-1", "looks good, carry on")])) is None


def test_agent_comments_and_other_tasks_never_open_it(ledger):
    assert find(ledger([comment("c-1", REQUEST, by=MASTER)], other=[comment("c-0", REQUEST)])) is None


def test_a_deleted_operator_comment_never_opens_it(ledger):
    assert find(ledger([{**comment("c-1", REQUEST), "deleted": True}])) is None


def test_an_operator_comment_counts_for_an_hour(ledger):
    assert find(ledger([comment("c-1", REQUEST, age=ledger_request.COMMENT_SEC - 1)])) == ("ledger", "c-1")
    assert find(ledger([comment("c-1", REQUEST, age=ledger_request.COMMENT_SEC)])) is None


def test_a_master_relay_opens_it_while_the_master_session_holds_the_typed_words(ledger):
    operator_words.record(MASTER, f"Approve the broker merge. {REQUEST}", now=NOW - 60)
    assert find(ledger([relay("c-1", f"Approve the broker merge. {REQUEST}")])) == ("relay", "c-1")


def test_a_relay_with_no_matching_typed_prompt_stays_refused(ledger):
    operator_words.record(MASTER, "approve the broker merge", now=NOW - 60)
    assert find(ledger([relay("c-1", REQUEST)])) is None


def test_a_relay_of_words_typed_thirty_minutes_ago_stays_refused(ledger):
    env = ledger([relay("c-1", REQUEST)])
    operator_words.record(MASTER, REQUEST, now=NOW - ledger_request.RELAY_SEC)
    assert find(env) is None
    operator_words.record(MASTER, REQUEST, now=NOW - ledger_request.RELAY_SEC + 1)
    assert find(env) == ("relay", "c-1")


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
    (tmp_path / "ledgers" / "demo.json").write_text("{not json")
    assert find(env) is None


def test_the_ledger_folder_defaults_to_the_home_development_ledger(tmp_path, monkeypatch):
    folder = tmp_path / "home" / "development-ledger"
    folder.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    tasks = [{"id": "t1", "comments": [comment("c-1", REQUEST)]}]
    (folder / "demo.json").write_text(json.dumps({"tasks": tasks, "_meta": {"members": {}}}))
    env = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_SWARM_TASK": "t1"}
    assert find(env) == ("ledger", "c-1")
