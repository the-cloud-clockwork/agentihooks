import builtins
import json
import time

import pytest

from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import affinity, capacity, launch_check, master_launch, master_start, runtime
from scripts.swarm import tick as tick_module
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig, SwarmError
from scripts.swarm.tick import Placed, SpawnError
from tests.swarm.test_capacity import account
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")
LAUNCH_CHECKED = True
ENDED = 1_791_374_400_000


class ResumingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.resumed, self.resume_fail, self.placed, self.configs, self.recovered = [], "", None, [], []
        self.store, self.spawn_states = None, []

    def resume(self, config, agent, text):
        if self.resume_fail:
            raise SpawnError(self.resume_fail)
        self.configs.append(config.slug)
        self.resumed.append((agent.name, agent.harness, agent.conversation_id, agent.profile, text))
        self.live.add(agent.name)
        return self.placed or Placed(
            pane_id="w2:m9",
            harness=agent.harness,
            account=agent.account,
            model="opus",
            effort="high",
            profile_decision={"validation": {"pid": 99}},
        )

    def spawn(self, config, lane, name, task):
        self.configs.append(config.slug)
        if self.store is not None:
            self.spawn_states.append(next(a.state for a in self.store.agents(config.slug) if a.name == name))
        return super().spawn(config, lane, name, task)

    def recover(self, name):
        self.recovered.append(name)
        return super().recover(name)


@pytest.fixture
def up(env, monkeypatch, tmp_path):  # noqa: F811
    from scripts.swarm import cli

    store, ledger, _ = env
    rt = ResumingRuntime()
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    ledger.joined, ledger.services = [], []
    ledger.join = lambda slug, name, role: ledger.joined.append((slug, name, role))
    monkeypatch.setattr(cli, "LedgerClient", lambda service=False: ledger.services.append(service) or ledger)
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


def gone_master(
    store,
    conversation="conv-1",
    harness="claude",
    profile="master",
    ended=ENDED,
    seat="master@sw",
    task=MASTER,
    **fields,
):
    name = store.next_name("sw", MASTER, 10)
    record = AgentRecord(
        name,
        MASTER,
        task,
        pane_id="w1:m1",
        harness=harness,
        conversation_id=conversation,
        profile=profile,
        seat=seat,
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


def test_a_third_try_still_counts(up, monkeypatch):
    _, _, rt = up
    asked = answers(monkeypatch, "maybe", "x", "2")
    assert run("sw", "master", "up") == 0
    assert len(asked) == 3 and len(rt.masters) == 1


def test_three_unclear_answers_start_nothing_and_name_the_flags(up, monkeypatch, capsys):
    store, _, rt = up
    asked = answers(monkeypatch, "maybe", "x", "y", "2")
    assert run("sw", "master", "up") == 1
    assert capsys.readouterr().err.strip() == "swarm: no clear answer after 3 tries; pass --last or --new"
    assert len(asked) == 3 and rt.masters == [] and masters(store) == []


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


def test_the_last_master_is_the_newest_one(up):
    store, _, _ = up
    gone_master(store, conversation="conv-old")
    newer = gone_master(store, conversation="conv-new", ended=ENDED + 1)
    assert master_launch.last_master(store, "sw").name == newer


def test_a_last_master_still_running_is_named_and_a_new_one_offered(up, monkeypatch, capsys):
    store, _, rt = up
    gone_master(store, conversation="conv-old")
    newer = gone_master(store, conversation="conv-new", ended=ENDED + 1)
    rt.live.add(newer)
    asked = answers(monkeypatch, "1", "y")
    assert run("sw", "master", "up") == 0
    out = capsys.readouterr().out
    assert f"Last master: {newer} on claude" in out
    assert "The last master cannot be resumed: it is still running" in out
    assert asked[-1] == master_launch.OFFER and rt.resumed == [] and len(rt.masters) == 1


def test_flag_last_with_the_last_master_still_running_exits_non_zero(up, monkeypatch, capsys):
    store, _, rt = up
    name = gone_master(store)
    rt.live.add(name)
    answers(monkeypatch)
    assert run("sw", "master", "up", "--last") == 1
    assert "the last master cannot be resumed: it is still running" in capsys.readouterr().err
    assert rt.resumed == [] and rt.masters == []


def test_a_dead_master_still_in_the_registry_is_the_last_master(up):
    store, _, _ = up
    gone_master(store)
    record = AgentRecord(
        "master@a1b2c3-0009", MASTER, MASTER, harness="claude", conversation_id="c9", started_at=ENDED + 5
    )
    store.put_agent("sw", record)
    assert master_launch.last_master(store, "sw").name == "master@a1b2c3-0009"


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
    monkeypatch.setattr(master_launch.agent_choice, "choose", lambda requested, environ: ("claude", "rotation"))
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


def test_a_new_master_that_has_not_reported_stays_starting_under_the_ticks_watch(up, monkeypatch):
    store, _, rt = up
    store.put_handoff("sw", MASTER, "# Handoff v2\n## Next\nGo on.\n")
    monkeypatch.setattr(rt, "reported", lambda agent: False)
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    [record] = masters(store)
    assert record.state == "starting"
    assert master_start.read(store, "sw")["name"] == record.name
    assert "Go on." in store.handoff("sw", MASTER)


def test_a_new_master_that_reported_leaves_no_pending_start(up, monkeypatch):
    store, _, _ = up
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    assert master_start.read(store, "sw") == {}


def test_a_failed_launch_keeps_the_ticks_pending_start(up, monkeypatch):
    store, _, rt = up
    earlier = {"name": "", "task": {"id": MASTER}, "attempt": 1, "retry": True, "at": 3}
    master_start.save(store, "sw", earlier)
    rt.fail = True
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 1
    assert master_start.read(store, "sw") == earlier


def test_a_failed_launch_leaves_no_pending_start_of_its_own(up, monkeypatch):
    store, _, rt = up
    rt.fail = True
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 1
    assert master_start.read(store, "sw") == {}


def test_no_master_started_leaves_a_stopped_swarm_and_closed_ledger_untouched(up, monkeypatch):
    store, ledger, _ = up
    store.update("sw", state="stopped")
    ledger.closed = lambda slug: True
    ledger.reopened = []
    ledger.reopen = lambda slug, by: ledger.reopened.append(by)
    answers(monkeypatch, "1", "n")
    assert run("sw", "master", "up") == 1
    assert store.config("sw").state == "stopped" and ledger.reopened == []


def test_a_closed_ledger_is_reopened_by_the_master_that_came_up(up, monkeypatch, capsys):
    store, ledger, _ = up
    ledger.closed = lambda slug: True
    ledger.reopened = []
    ledger.reopen = lambda slug, by: ledger.reopened.append((slug, by))
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    assert ledger.reopened == [("sw", printed(capsys)["master"])]


AT = 1_791_380_000_000


def direct(store, rt, choice, *replies):
    queue, said = list(replies), []

    def ask(question):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return master_launch.up(store, "sw", rt, AT, choice, ask, said.append), said


def history_row(store, name, lane, ended):
    store.put_agent("sw", AgentRecord(name, lane, lane, harness="claude", conversation_id="c", started_at=1))
    store.drop_agent("sw", name, at=ended)


@pytest.fixture
def india(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_no_master_at_all_has_no_last_master(up):
    store, _, _ = up
    assert master_launch.last_master(store, "sw") is None


def test_every_history_row_is_read_for_the_last_master(up):
    store, _, _ = up
    history_row(store, "engineer@a1b2c3-0001", "eng", ENDED - 2)
    history_row(store, "engineer@a1b2c3-0002", "eng", ENDED - 1)
    name = gone_master(store)
    assert master_launch.last_master(store, "sw").name == name


def test_a_later_row_of_another_lane_is_never_the_last_master(up):
    store, _, _ = up
    name = gone_master(store)
    history_row(store, "engineer@a1b2c3-0001", "eng", ENDED + 5)
    assert master_launch.last_master(store, "sw").name == name


def test_describe_words_and_the_time_in_utc(india):
    assert master_launch.describe(None) == "No earlier master is recorded for this swarm."
    unknown = master_launch.Previous(AgentRecord("m", MASTER, MASTER), ENDED)
    assert master_launch.describe(unknown) == "Last master: m on an unknown harness, last ran 2026-10-07 12:00 UTC"


def test_the_refusals_read_exactly(up, monkeypatch, capsys):
    store, _, _ = up
    answers(monkeypatch)
    assert run("sw", "master", "up") == 1
    assert capsys.readouterr().err.strip() == "swarm: no answer on standard input; pass --last or --new"
    gone_master(store, conversation="")
    answers(monkeypatch, "1", "n")
    assert run("sw", "master", "up") == 1
    assert capsys.readouterr().err.strip() == "swarm: no master started"


def test_fill_takes_every_empty_key_from_a_bare_config(monkeypatch):
    from scripts import agent_choice

    monkeypatch.setattr(agent_choice, "choose", lambda requested, environ: ("claude", "rotation"))
    bare = SwarmConfig("sw", "/repo", 0, 0)
    assert master_launch.fill({}, bare) == {"profile": "master", "harness": "claude", "model": "opus", "effort": "high"}


def test_fill_keeps_a_saved_harness_and_picks_its_frontier_model():
    config = SwarmConfig("sw", "/repo", 0, 0, lanes={MASTER: {"agent": "claude"}})
    assert master_launch.fill({"harness": "codex"}, config)["model"] == "gpt-6.1-sol"


def test_a_partial_handoff_launch_keeps_its_values_and_the_envelope():
    config = SwarmConfig("sw", "/repo", 0, 0)
    task = {"id": MASTER, "handoff_envelope": {"reason": "recycle", "launch": {"harness": "codex", "account": "a9"}}}
    filled = master_launch._filled(task, config)
    assert filled["handoff_envelope"] == {
        "reason": "recycle",
        "launch": {"profile": "master", "harness": "codex", "model": "gpt-6.1-sol", "effort": "high", "account": "a9"},
    }


def test_a_task_without_handoff_or_saved_launch_is_left_alone():
    task = {"id": MASTER, "handoff": "", "handoff_envelope": {"reason": "recycle"}}
    assert master_launch._filled(task, SwarmConfig("sw", "/repo", 0, 0)) == task


def test_a_resumed_master_without_launch_facts_keeps_its_own(up):
    store, _, rt = up
    store.update("sw", lanes={**store.config("sw").lanes, MASTER: {"profile": "boss"}})
    name = gone_master(
        store,
        harness="codex",
        profile="",
        account="acct-1",
        model="old-m",
        effort="low",
        idle_ticks=5,
        task="",
        seat="",
    )
    rt.placed = Placed(pane_id="w2:m4", harness="")
    launched, _ = direct(store, rt, master_launch.LAST)
    [record] = masters(store)
    assert (record.harness, record.account, record.model, record.effort, record.profile) == (
        "codex",
        "acct-1",
        "old-m",
        "low",
        "boss",
    )
    assert (record.started_at, record.idle_ticks, record.task, record.seat) == (AT, 0, MASTER, "master@sw")
    assert store.seats.history("master@sw")[-1]["at"] == AT
    assert rt.configs == ["sw"]
    text = rt.resumed[0][4]
    assert f"agentihooks swarm sw master up as {name}" in text and master_launch.ledger_source("sw") in text
    assert launched == master_launch.Launched(name, "w2:m4", "master@sw", master_launch.LAST)


def test_a_resumed_master_takes_the_launch_facts_it_reports(up):
    store, _, rt = up
    gone_master(store, harness="codex", account="acct-1", model="old-m", effort="low")
    rt.placed = Placed("w2:m5", "claude", "acct-2", "opus-new", "medium", profile="master-v2")
    direct(store, rt, master_launch.LAST)
    [record] = masters(store)
    assert (record.harness, record.account, record.model, record.effort, record.profile) == (
        "claude",
        "acct-2",
        "opus-new",
        "medium",
        "master-v2",
    )


def test_a_new_master_is_named_seated_and_primed_at_launch_time(up):
    store, _, rt = up
    rt.store = store
    store.set_peer("sw", "peer-x")
    old = AgentRecord("master@a1b2c3-0099", MASTER, MASTER, seat="master@sw")
    pending = transfers.record(store, "sw", old, "recycle", "# Handoff v2\n## Next\nGo.\n", 5)
    store.redis.hset(store.key("sw", "launch-assignments"), MASTER, json.dumps({"profile": "master"}))
    launched, _ = direct(store, rt, master_launch.NEW)
    name = launched.master
    [(_, task)] = rt.masters
    assert store.names.entry(name)["spawned_at"] == AT
    assert store.seats.history("master@sw")[-1]["at"] == AT
    assert (task["peer"], task["transfer"]["id"], task["transfer"]["successor"]) == ("peer-x", pending["id"], name)
    assert rt.spawn_states == ["starting"] and rt.configs == ["sw"]
    assert masters(store)[0].started_at == AT
    assert store.redis.hget(store.key("sw", "launch-assignments"), MASTER) is None
    assert launch_check.pending(store, "sw") == {name: {"task": MASTER, "at": AT, "relaunch": True}}


def test_an_unreported_new_master_is_watched_from_launch_time(up, monkeypatch):
    store, _, rt = up
    monkeypatch.setattr(rt, "reported", lambda agent: False)
    launched, _ = direct(store, rt, master_launch.NEW)
    started = master_start.read(store, "sw")
    assert (started["name"], started["task"]["id"], started["at"]) == (launched.master, MASTER, AT)


def order_item(store):
    return InboxStore(store.redis).send("operator", "master@sw", "hand off to claude").id


def test_a_failed_new_master_marks_its_transfer_absent_and_the_affinity_order_failed(up):
    store, _, rt = up
    old = AgentRecord("master@a1b2c3-0099", MASTER, MASTER, seat="master@sw")
    pending = transfers.record(store, "sw", old, "recycle", "# Handoff v2\n## Next\nGo.\n", 5)
    order = {
        "to": "claude",
        "from": "codex",
        "master": old.name,
        "item": order_item(store),
        "at": 1,
        "state": "ordered",
    }
    store.redis.set(store.key("sw", "master-affinity"), json.dumps(order))
    rt.fail = True
    with pytest.raises(SwarmError):
        direct(store, rt, master_launch.NEW)
    assert transfers.get(store, "sw", pending["id"])["binding"]["state"] == "absent"
    found = affinity.pending(store, "sw")
    assert (found["state"], found["reason"]) == ("failed", "herdr down")


def test_a_new_master_on_the_ordered_harness_closes_the_affinity_order(up):
    store, _, rt = up
    order = {"to": "claude", "from": "codex", "master": "x", "item": order_item(store), "at": 1, "state": "ordered"}
    store.redis.set(store.key("sw", "master-affinity"), json.dumps(order))
    direct(store, rt, master_launch.NEW)
    assert affinity.pending(store, "sw") is None


def test_the_tick_recovers_a_live_master_by_its_name(up):
    store, _, rt = up
    name = store.next_name("sw", MASTER, 1)
    rt.live.add(name)
    tick_module._recover_master("sw", store.ensure_code("sw"), store, rt, AT)
    assert rt.recovered == [name]


def test_an_unknown_swarm_is_recreated_from_its_snapshot_first(up, monkeypatch, tmp_path):
    from scripts.swarm import cli

    store, _, rt = up
    made = []

    def recreate(given, slug, live):
        made.append((given, slug, live))
        store.create(SwarmConfig(slug, str(tmp_path), 0, 0))

    monkeypatch.setattr(cli.snapshot, "recreate", recreate)
    answers(monkeypatch)
    assert run("gone", "master", "up", "--new") == 0
    assert made == [(store, "gone", set())] and rt.live


@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_a_stopped_or_stopping_swarm_is_paused_and_its_timer_ensured(up, monkeypatch, state):
    from scripts.swarm import cli

    store, ledger, _ = up
    ensured, asked = [], []
    ledger.services.clear()
    monkeypatch.setattr(cli.timer, "entry_point", lambda: "/installed/agentihooks")
    monkeypatch.setattr(cli.timer, "ensure", lambda binary: ensured.append(binary) or True)
    ledger.closed = lambda slug: asked.append(slug) or False
    store.update("sw", state=state)
    answers(monkeypatch)
    assert run("sw", "master", "up", "--new") == 0
    assert store.config("sw").state == "paused"
    assert ensured == ["/installed/agentihooks"] and asked == ["sw"]
    assert ledger.services == [True]


def test_the_master_command_needs_its_up_action():
    from scripts.swarm import cli

    parsed = cli.build_parser().parse_args(["sw", "master", "up"])
    assert (parsed.action, parsed.choice) == ("up", "")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "master"])


def test_a_new_master_reads_quota_before_it_is_placed(up, monkeypatch):
    store, _, rt = up
    order = []
    monkeypatch.setattr(
        rt, "quota_capacity", lambda config, agents, now: order.append(("quota", config.slug, now)), raising=False
    )
    spawn = rt.spawn
    monkeypatch.setattr(rt, "spawn", lambda *args: order.append("spawn") or spawn(*args))
    direct(store, rt, master_launch.NEW)
    assert order == [("quota", "sw", AT / 1000), "spawn"]


def quota_master(monkeypatch, tmp_path, observed):
    monkeypatch.setattr(capacity, "accounts", lambda environ, now, refresh=True: observed)
    rt = runtime.HerdrRuntime(
        home=tmp_path / "home", choose=lambda requested, environ: (requested or "claude", "priority")
    )
    monkeypatch.setattr(
        runtime.profile_choice,
        "choose",
        lambda slug, lane, chosen, task, environ, overlays=None: runtime.profile_choice.ProfileDecision(
            "master", "task", "explicit"
        ),
    )
    monkeypatch.setattr(rt, "live_names", set)
    monkeypatch.setattr(rt, "reported", lambda agent: True)
    routes = []

    def launch(cfg, lane, task, name, argv, **kwargs):
        route = argv[argv.index("--route") + 1] if "--route" in argv else ""
        routes.append(route)
        return runtime.Placed("pane", argv[argv.index("--agent") + 1], route)

    monkeypatch.setattr(rt, "_launch", launch)
    return rt, routes


def test_a_new_master_leaves_a_warned_account_or_refuses_naming_it(up, monkeypatch, tmp_path):
    store, _, _ = up
    rt, routes = quota_master(monkeypatch, tmp_path, [account("w", left=5), account("ok")])
    direct(store, rt, master_launch.NEW)
    assert routes == ["ok"]
    rt, routes = quota_master(monkeypatch, tmp_path, [account("w", left=5)])
    refusal = "no claude or codex account has placeable quota seats: claude w is at its week quota warning"
    with pytest.raises(SwarmError, match=f"^the new master could not start: {refusal}$"):
        direct(store, rt, master_launch.NEW)
    assert routes == []


def test_a_new_master_free_to_pick_its_harness_refuses_naming_every_warned_account(up, monkeypatch, tmp_path):
    store, _, _ = up
    rt, routes = quota_master(monkeypatch, tmp_path, [account("w", left=5), account("cw", harness="codex", left=5)])
    warned = "claude w is at its week quota warning; codex cw is at its week quota warning"
    with pytest.raises(
        SwarmError,
        match=f"^the new master could not start: no claude or codex account has placeable quota seats: {warned}$",
    ):
        direct(store, rt, master_launch.NEW)
    assert routes == []
