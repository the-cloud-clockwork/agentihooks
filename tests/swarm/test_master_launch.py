import builtins
import json

import pytest

from scripts.swarm import master_launch, runtime
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import Placed, SpawnError
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")
ENDED = 1_791_374_400_000


class ResumingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.resumed, self.resume_fail = [], ""

    def resume(self, config, agent, text):
        if self.resume_fail:
            raise SpawnError(self.resume_fail)
        self.resumed.append((agent.name, agent.harness, agent.conversation_id, agent.profile, text))
        self.live.add(agent.name)
        return Placed(
            pane_id="w2:m9",
            harness=agent.harness,
            account=agent.account,
            model="opus",
            effort="high",
            profile_decision={"validation": {"pid": 99}},
        )


@pytest.fixture
def up(env, monkeypatch, tmp_path):  # noqa: F811
    from scripts.swarm import cli

    store, ledger, _ = env
    rt = ResumingRuntime()
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    ledger.joined = []
    ledger.join = lambda slug, name, role: ledger.joined.append((slug, name, role))
    ledger.closed = lambda slug: False
    run("sw", "create", "--repo", str(tmp_path), "--max-eng-agents", "0", "--max-ci-agents", "0")
    return store, ledger, rt


def answers(monkeypatch, *replies):
    asked, queue = [], list(replies)

    def ask(question=""):
        asked.append(question)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", ask)
    return asked


def gone_master(store, conversation="conv-1", harness="claude", profile="master", ended=ENDED, **fields):
    name = store.next_name("sw", MASTER, 10)
    record = AgentRecord(
        name,
        MASTER,
        MASTER,
        pane_id="w1:m1",
        harness=harness,
        conversation_id=conversation,
        profile=profile,
        seat="master@sw",
        started_at=10,
        **fields,
    )
    store.put_agent("sw", record)
    store.drop_agent("sw", name, at=ended)
    return name


def masters(store):
    return [a for a in store.agents("sw") if a.lane == MASTER]


def printed(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_prompt_shows_the_last_master_and_asks_last_or_new(up, monkeypatch, capsys):
    store, _, rt = up
    name = gone_master(store, harness="codex")
    asked = answers(monkeypatch, "1")
    assert run("sw", "master", "up") == 0
    out = capsys.readouterr().out
    assert f"Last master: {name} on codex, last ran 2026-10-07 12:00 UTC" in out
    assert "1) bring back the last master" in out and "2) start a new master" in out
    assert asked == ["Choose 1 or 2: "]
    assert [r[0] for r in rt.resumed] == [name]


def test_an_unclear_answer_is_asked_again(up, monkeypatch):
    store, _, rt = up
    gone_master(store)
    asked = answers(monkeypatch, "maybe", "new")
    assert run("sw", "master", "up") == 0
    assert len(asked) == 2 and len(rt.masters) == 1 and rt.resumed == []


def test_no_answer_on_standard_input_names_the_flags(up, monkeypatch, capsys):
    store, _, rt = up
    answers(monkeypatch)
    assert run("sw", "master", "up") == 1
    assert "pass --last or --new" in capsys.readouterr().err
    assert rt.masters == [] and masters(store) == []


def test_choice_last_reopens_the_last_masters_own_conversation_in_the_master_seat(up, monkeypatch, capsys):
    store, ledger, rt = up
    name = gone_master(
        store, harness="codex", conversation="conv-7", account="acct-2", profile_decision={"validation": {"pid": 1}}
    )
    answers(monkeypatch, "1")
    assert run("sw", "master", "up") == 0
    [(resumed, harness, conversation, profile, text)] = rt.resumed
    assert (resumed, harness, conversation, profile) == (name, "codex", "conv-7", "master")
    assert "master up" in text and "re-read the ledger" in text
    [record] = masters(store)
    assert (record.name, record.state, record.pane_id, record.seat) == (name, "working", "w2:m9", "master@sw")
    assert record.conversation_id == "conv-7" and record.account == "acct-2" and record.harness == "codex"
    assert record.profile == "master" and record.profile_decision == {"validation": {"pid": 99}}
    assert store.seats.occupant("master@sw").occupant == name
    assert ledger.joined == [("sw", name, "orchestrator")]
    assert printed(capsys) == {"master": name, "pane": "w2:m9", "seat": "master@sw", "choice": "last"}
    assert rt.masters == []


def test_choice_new_launches_a_fresh_master_reading_the_seat_handoff_recap_and_learned(up, monkeypatch, capsys):
    store, ledger, rt = up
    gone_master(store)
    store.put_handoff("sw", MASTER, "# Handoff v2\n## Next\nAnswer the operator.\n")
    store.memory.add_recap("master@sw", "old", MASTER, "recap text", 5)
    store.memory.learn("master@sw", "old", "lesson because reason", 6)
    answers(monkeypatch, "2")
    assert run("sw", "master", "up") == 0
    [(name, task)] = rt.masters
    assert task["id"] == MASTER and task["seat"] == "master@sw"
    assert "Answer the operator." in task["handoff"]
    assert [r["text"] for r in task["recaps"]] == ["recap text"]
    assert [n["text"] for n in task["learned"]] == ["lesson because reason"]
    [record] = masters(store)
    assert (record.name, record.state, record.pane_id) == (name, "working", "w1:m1")
    assert store.seats.occupant("master@sw").occupant == name
    assert store.handoff("sw", MASTER) == ""
    assert ledger.joined == [("sw", name, "orchestrator")]
    assert printed(capsys) == {"master": name, "pane": "w1:m1", "seat": "master@sw", "choice": "new"}
    assert rt.resumed == []


def test_flag_last_answers_without_a_prompt(up, monkeypatch):
    store, _, rt = up
    name = gone_master(store)
    asked = answers(monkeypatch)
    assert run("sw", "master", "up", "--last") == 0
    assert asked == [] and [r[0] for r in rt.resumed] == [name]


def test_flag_new_answers_without_a_prompt(up, monkeypatch):
    store, _, rt = up
    gone_master(store)
    asked = answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    assert asked == [] and len(rt.masters) == 1 and rt.resumed == []


def test_last_and_new_flags_exclude_each_other(up):
    with pytest.raises(SystemExit):
        run("sw", "master", "up", "--last", "--new")


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"conversation": ""}, "no conversation id"),
        ({"harness": ""}, "its harness is not recorded"),
    ],
)
def test_a_last_master_that_cannot_be_resumed_says_why_and_offers_a_new_one(up, monkeypatch, capsys, fields, reason):
    store, _, rt = up
    gone_master(store, **fields)
    asked = answers(monkeypatch, "1", "y")
    assert run("sw", "master", "up") == 0
    assert f"The last master cannot be resumed: {reason}" in capsys.readouterr().out
    assert asked[-1] == "Start a new master instead? [y/N] "
    assert rt.resumed == [] and len(rt.masters) == 1


def test_declining_the_new_master_starts_nothing_and_exits_non_zero(up, monkeypatch, capsys):
    store, _, rt = up
    gone_master(store, conversation="")
    answers(monkeypatch, "1", "n")
    assert run("sw", "master", "up") == 1
    assert "no master started" in capsys.readouterr().err
    assert rt.masters == [] and masters(store) == []


def test_no_recorded_master_cannot_be_brought_back(up, monkeypatch, capsys):
    store, _, rt = up
    answers(monkeypatch, "1", "y")
    assert run("sw", "master", "up") == 0
    out = capsys.readouterr().out
    assert "No earlier master is recorded for this swarm." in out
    assert "The last master cannot be resumed: no earlier master is recorded" in out
    assert len(rt.masters) == 1


def test_a_resume_that_does_not_start_says_why_and_offers_a_new_one(up, monkeypatch, capsys):
    store, _, rt = up
    name = gone_master(store)
    rt.resume_fail = "herdr never showed conversation conv-1"
    answers(monkeypatch, "1", "yes")
    assert run("sw", "master", "up") == 0
    assert "cannot be resumed: resume did not start: herdr never showed conversation conv-1" in capsys.readouterr().out
    assert [a.name for a in masters(store)] != [name] and len(rt.masters) == 1


def test_flag_last_that_cannot_resume_exits_non_zero_without_switching(up, monkeypatch, capsys):
    store, _, rt = up
    gone_master(store, conversation="")
    asked = answers(monkeypatch)
    assert run("sw", "master", "up", "--last") == 1
    err = capsys.readouterr().err
    assert "the last master cannot be resumed: no conversation id" in err and "--new" in err
    assert asked == [] and rt.masters == [] and masters(store) == []


def test_the_last_master_is_the_newest_one_not_running(up, monkeypatch):
    store, _, rt = up
    older = gone_master(store, conversation="conv-old")
    newer = gone_master(store, conversation="conv-new", ended=ENDED + 1)
    rt.live.add(newer)
    assert master_launch.last_master(store, "sw", {newer}).name == older
    assert master_launch.last_master(store, "sw", set()).name == newer


def test_a_dead_master_still_in_the_registry_is_the_last_master(up):
    store, _, _ = up
    gone_master(store)
    record = AgentRecord(
        "master@a1b2c3-0009", MASTER, MASTER, harness="claude", conversation_id="c9", started_at=ENDED + 5
    )
    store.put_agent("sw", record)
    assert master_launch.last_master(store, "sw", set()).name == "master@a1b2c3-0009"


def test_a_live_master_present_does_not_stop_a_new_master_and_both_stay_seated(up, monkeypatch):
    store, _, rt = up
    live = AgentRecord("master@a1b2c3-0005", MASTER, MASTER, harness="claude", seat="master@sw", started_at=5)
    store.put_agent("sw", live)
    rt.live.add(live.name)
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    found = masters(store)
    assert len(found) == 2 and all(a.state != "finished" and a.seat == "master@sw" for a in found)


def test_an_empty_saved_assignment_launches_from_the_swarm_config(up, monkeypatch):
    store, _, rt = up
    store.redis.hset(store.key("sw", "launch-assignments"), MASTER, json.dumps({"profile": "", "harness": ""}))
    store.put_handoff("sw", MASTER, "# Handoff v2\n## Next\nGo on.\n")
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    [(_, task)] = rt.masters
    launch = {"profile": "master", "harness": "claude", "model": "opus", "effort": "high"}
    assert {k: task["launch_assignment"][k] for k in launch} == launch
    assert {k: task["handoff_envelope"]["launch"][k] for k in launch} == launch
    assert runtime._transfer(task)["harness"] == "claude"


def test_an_incomplete_saved_assignment_keeps_what_it_has_and_fills_the_rest(up, monkeypatch):
    store, _, rt = up
    run("sw", "set", "master-agent=codex", "effort-max=medium")
    saved = {"profile": "master", "harness": "", "model": "", "effort": "", "account": "acct-3"}
    store.redis.hset(store.key("sw", "launch-assignments"), MASTER, json.dumps(saved))
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    [(_, task)] = rt.masters
    assert task["launch_assignment"] == {
        "profile": "master",
        "harness": "codex",
        "model": "gpt-6.1-sol",
        "effort": "medium",
        "account": "acct-3",
    }


def test_no_saved_assignment_leaves_the_launch_to_the_profile_choice(up, monkeypatch):
    store, _, rt = up
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    [(_, task)] = rt.masters
    assert "launch_assignment" not in task and not (task.get("handoff_envelope") or {}).get("launch")


def test_a_resumed_master_without_a_recorded_profile_takes_the_swarm_master_profile(up, monkeypatch):
    store, _, rt = up
    run("sw", "set", "master-profile=boss")
    gone_master(store, profile="")
    answers(monkeypatch)
    assert run("sw", "master", "up", "--last") == 0
    assert rt.resumed[0][3] == "boss"


def test_a_failed_launch_prints_the_reason_and_exits_non_zero(up, monkeypatch, capsys):
    store, ledger, rt = up
    rt.fail = True
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 1
    assert "the new master could not start: herdr down" in capsys.readouterr().err
    assert masters(store) == [] and ledger.joined == []


def test_a_stopped_swarm_is_paused_so_the_tick_keeps_the_master(up, monkeypatch):
    store, _, _ = up
    store.update("sw", state="stopped")
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    assert store.config("sw").state == "paused"
