import io
import json
import os

import pytest

from scripts.gates import Call, Gate, Who
from scripts.gates import quiet as gate
from scripts.gates.progress import Progress
from scripts.gates.verdicts import Verdicts
from scripts.swarm import idle
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger import ledger_workspace

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG, ME, OTHER = "demo", "engineer@100001-0001", "engineer@100001-0002"
MIN = 60_000
NOW = 100 * MIN
WHO = Who(name=ME, swarm=SLUG, lane="eng", task="t1")
FLAG = "quiet for 30 minutes on task t1: run agentihooks swarm demo progress"


def bash(command):
    return Call("Bash", {"command": command})


class FakeLedger:
    def __init__(self):
        self.comments = []

    def comment(self, slug, task_id, text, by):
        self.comments.append((slug, task_id, text, by))


@pytest.fixture
def rig(tmp_path):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent(SLUG, AgentRecord(name=ME, lane="eng", task="t1", started_at=NOW - 40 * MIN))
    rows = {"t1": {"id": "t1", "state": "claimed", "claimed_by": ME}}
    flags = Verdicts(SLUG, gate.NAME, tmp_path)

    def run(now=NOW):
        return gate.quiet_pass(store, SLUG, rows, now, tmp_path)

    def minutes(now=NOW):
        return gate.quiet_minutes(store.redis, SLUG, store.agents(SLUG), rows, now)

    def log_rows():
        path = tmp_path / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    rig = type("Rig", (), {})()
    rig.store, rig.rows, rig.flags, rig.run, rig.minutes, rig.log_rows, rig.home = (
        store,
        rows,
        flags,
        run,
        minutes,
        log_rows,
        tmp_path,
    )
    return rig


def test_it_is_a_tool_call_gate_that_ships_enforcing():
    quiet = gate.QuietClaim()
    assert isinstance(quiet, Gate)
    assert (quiet.name, quiet.default_mode) == ("quiet", "enforce")
    assert quiet.matches(Call("Read")) and quiet.matches(bash("ls"))
    assert not quiet.matches(Call(""))


def test_no_flag_lets_every_call_through(rig):
    assert gate.QuietClaim().decide(Call("Read"), WHO, rig.flags).allowed


def test_a_standing_flag_refuses_every_call_but_the_ledger_swarm_and_msg_commands(rig):
    rig.flags.write(ME, "quiet", FLAG)
    quiet = gate.QuietClaim()
    for call in (Call("Read"), Call("Edit"), bash("ls"), bash(""), bash("agentihooks status")):
        decision = quiet.decide(call, WHO, rig.flags)
        assert (decision.allowed, decision.reason) == (False, FLAG), call
    denied = quiet.decide(bash(f"agentihooks ledger --slug {SLUG} comment phases/p1 x && ls"), WHO, rig.flags)
    assert not denied.allowed
    for command in (
        f"agentihooks ledger --slug {SLUG} --as {ME} comment tasks/t1 'still on it'",
        f"agentihooks swarm {SLUG} progress --doing tests --ends-when green && agentihooks msg inbox",
    ):
        assert quiet.decide(bash(command), WHO, rig.flags).allowed, command
    assert not quiet.decide(Call("Read", {"command": "agentihooks msg inbox"}), WHO, rig.flags).allowed


def test_a_session_outside_a_swarm_is_never_refused(rig):
    rig.flags.write(ME, "quiet", FLAG)
    assert gate.QuietClaim().decide(Call("Read"), Who(name=ME), rig.flags).allowed


def test_a_malformed_flag_fails_open_through_the_gate_entry_and_is_counted(tmp_path):
    from scripts.gates import entry, log

    flags = Verdicts(SLUG, gate.NAME, tmp_path)
    flags.path(ME).parent.mkdir(parents=True)
    flags.path(ME).write_text('{"verdict": "quiet"}')
    env = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": ME}
    assert entry.main(["quiet"], io.StringIO(json.dumps({"tool_name": "Read"})), env, tmp_path) == 0
    assert [(r["gate"], r["kind"], r["tool"]) for r in log.recent(SLUG, home=tmp_path)] == [
        ("quiet", "fail-open", "Read")
    ]


def test_quiet_minutes_run_from_the_latest_progress_signal(rig):
    assert rig.minutes() == {ME: 40}
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", NOW - 10 * MIN)
    assert rig.minutes() == {ME: 10}
    path = ledger_workspace.folder(SLUG, "t1") / "progress.md"
    path.parent.mkdir(parents=True)
    path.write_text("tests written\n")
    os.utime(path, (0, (NOW - 5 * MIN) / 1000))
    assert rig.minutes() == {ME: 5}
    assert rig.minutes(NOW - 6 * MIN) == {ME: -1}


def test_an_agent_skipped_first_never_stops_the_next_from_being_measured(rig):
    me = rig.store.agents(SLUG)[0]
    planner = AgentRecord(name="planner@100001-0001", lane="plan", task="t1", started_at=1)
    assert gate.quiet_minutes(rig.store.redis, SLUG, [planner, me], rig.rows, NOW) == {ME: 40}


def test_a_checked_wait_is_never_quiet_and_a_bare_or_ended_one_is(rig):
    redis = rig.store.redis
    idle.declare_wait(redis, SLUG, ME, NOW + MIN, "t2 first", NOW - MIN, on={"kind": "task", "target": "t2"})
    assert rig.minutes() == {}
    assert rig.minutes(NOW + MIN) == {ME: 41}
    idle.declare_wait(redis, SLUG, ME, NOW + MIN, "thinking", NOW - MIN)
    assert rig.minutes() == {ME: 40}


@pytest.mark.parametrize(
    "change",
    [
        {"state": "open"},
        {"state": "done"},
        {"claimed_by": OTHER},
    ],
)
def test_only_the_holder_of_a_claimed_or_pr_task_can_go_quiet(rig, change):
    rig.rows["t1"].update(change)
    assert rig.minutes() == {}


@pytest.mark.parametrize(
    "record",
    [{"lane": "plan"}, {"lane": "master"}, {"state": "finished"}, {"state": "awaiting-decision"}, {"task": "t9"}],
)
def test_only_a_live_worker_can_go_quiet(rig, record):
    agent = rig.store.agents(SLUG)[0]
    rig.store.put_agent(SLUG, AgentRecord(**{**agent.__dict__, **record}))
    assert rig.minutes() == {}


def test_a_pr_task_holder_goes_quiet_too(rig):
    rig.rows["t1"]["state"] = "pr"
    assert rig.minutes() == {ME: 40}


def test_an_agent_with_no_signal_at_all_is_not_measured(rig):
    agent = rig.store.agents(SLUG)[0]
    rig.store.put_agent(SLUG, AgentRecord(**{**agent.__dict__, "started_at": 0}))
    assert rig.minutes() == {}
    rig.store.put_agent(SLUG, AgentRecord(**{**agent.__dict__, "started_at": 1}))
    assert rig.minutes() == {ME: (NOW - 1) // MIN}


def test_the_tick_raises_the_flag_at_thirty_minutes_once_and_clears_it_on_progress(rig):
    assert rig.run(NOW - 11 * MIN) == []
    assert rig.flags.read(ME) is None
    assert rig.run() == [f"raised the quiet flag on {ME}: 40 minutes with no progress on task t1"]
    flag = rig.flags.read(ME)
    reason = (
        "quiet for 40 minutes on task t1: run agentihooks swarm demo progress --doing <what> --ends-when <what> "
        "to say what you are doing and when it ends"
    )
    assert flag == {"verdict": "quiet", "reason": reason, "at": NOW}
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"], r["at"]) for r in rig.log_rows()] == [
        ("quiet", "count", ME, "t1", reason, NOW)
    ]
    assert rig.run(NOW + MIN) == []
    assert len(rig.log_rows()) == 1
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", NOW + MIN)
    assert rig.run(NOW + 2 * MIN) == [f"cleared the quiet flag on {ME}"]
    assert rig.flags.read(ME) is None


def test_the_tick_raises_the_flag_at_exactly_thirty_minutes_and_keeps_it(rig):
    assert rig.run(NOW - 10 * MIN - 1) == []
    assert rig.run(NOW - 10 * MIN)[0].startswith(f"raised the quiet flag on {ME}: 30 minutes")
    assert rig.run(NOW - 10 * MIN) == []
    assert rig.flags.read(ME) is not None


def test_an_agent_already_flagged_never_stops_the_next_from_being_raised(rig):
    rig.store.put_agent(SLUG, AgentRecord(name=OTHER, lane="eng", task="t2", started_at=NOW - 50 * MIN))
    rig.rows["t2"] = {"id": "t2", "state": "claimed", "claimed_by": OTHER}
    rig.flags.write(ME, "quiet", FLAG)
    assert rig.run() == [f"raised the quiet flag on {OTHER}: 50 minutes with no progress on task t2"]
    assert rig.flags.read(ME)["reason"] == FLAG


def test_the_tick_clears_a_flag_whose_agent_left_the_swarm(rig):
    rig.flags.write(OTHER, "quiet", FLAG)
    rig.flags.path(ME).parent.joinpath(".staged").write_text("{}")
    rig.rows["t1"]["state"] = "done"
    assert rig.run() == [f"cleared the quiet flag on {OTHER}"]
    assert rig.flags.read(OTHER) is None


def test_progress_writes_the_ledger_and_progress_lines_records_an_outcome_and_clears_the_flag(rig):
    ledger, agent = FakeLedger(), rig.store.agents(SLUG)[0]
    rig.flags.write(ME, "quiet", FLAG)
    line = gate.report(rig.store, ledger, SLUG, agent, gate.Status("writing the tests", "they pass"), NOW, rig.home)
    assert line == "Doing writing the tests. Done when they pass."
    assert ledger.comments == [(SLUG, "t1", line, ME)]
    progress = (ledger_workspace.folder(SLUG, "t1") / "progress.md").read_text()
    assert progress == f"1970-01-01T01:40:00+00:00 {line}\n"
    mark = Progress(rig.store.redis, SLUG).read(ME)
    assert (mark.outcome_at, mark.outcome, mark.talk) == (NOW, gate.STATUS, 0)
    assert rig.flags.read(ME) is None
    gate.report(rig.store, ledger, SLUG, agent, gate.Status("reviewing", "both readers close"), NOW, rig.home)
    assert len((ledger_workspace.folder(SLUG, "t1") / "progress.md").read_text().splitlines()) == 2
