"""The operator's page lift: a swarm command arms a gate lift for one agent and logs it like the typed lift."""

import io
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.gates import entry, lift, log
from scripts.gates.base import Who
from scripts.swarm import cli, status
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.gates.test_runtime import SID, Stub

SLUG, ME, TASK = "sw", "engineer@1-1", "t1"
WHO = Who(name=ME, swarm=SLUG, task=TASK)
HOUR_MS = lift.LIFT_SECONDS * 1000
URL, SUCCESSOR = "https://github.com/o/r/pull/7", "engineer@2-2"
LONG = "x" * 400


def saved():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/r", 1, 0))
    store.put_agent(SLUG, AgentRecord(name=ME, lane="eng", task=TASK))
    return store


def rows(home=None):
    return [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"]) for r in log.recent(SLUG, home=home)]


class TestLiftAgent:
    def test_arms_the_agent_and_logs_the_typed_lift_row(self, tmp_path):
        lift.lift_agent(WHO, "watch", tmp_path, now=100.0)
        assert lift.agent_lifted(SLUG, ME, "watch", tmp_path, now=100.0 + lift.LIFT_SECONDS - 1)
        assert not lift.agent_lifted(SLUG, ME, "watch", tmp_path, now=100.0 + lift.LIFT_SECONDS)
        assert not lift.agent_lifted(SLUG, "engineer@2-2", "watch", tmp_path, now=101.0)
        assert not lift.agent_lifted(SLUG, ME, "talk", tmp_path, now=101.0)
        assert rows(tmp_path) == [("watch", "lift", ME, TASK, lift.LIFT_REASON)]

    def test_agent_lifts_live_apart_from_session_lifts(self, tmp_path):
        lift.lift_agent(WHO, "watch", tmp_path)
        assert not lift.lifted(SLUG, ME, "watch", tmp_path)
        lift.arm(SLUG, ME, "talk", tmp_path)
        assert not lift.agent_lifted(SLUG, ME, "talk", tmp_path)

    def test_nameless_agent_or_swarm_reads_not_lifted_even_when_armed(self, tmp_path):
        lift.lift_agent(Who(name="", swarm=SLUG), "watch", tmp_path)
        lift.lift_agent(Who(name=ME, swarm=""), "watch", tmp_path)
        assert not lift.agent_lifted(SLUG, "", "watch", tmp_path)
        assert not lift.agent_lifted("", ME, "watch", tmp_path)

    def test_agent_lift_path_sits_under_the_swarm_home_lifts_agents_folder(self, tmp_path):
        path = lift.agent_lift_path(SLUG, ME, "watch", tmp_path)
        assert path == tmp_path / SLUG / "gates" / "lifts" / "agents" / ME / "watch"

    def test_a_second_lift_rearms_the_same_gate(self, tmp_path):
        lift.lift_agent(WHO, "watch", tmp_path, now=100.0)
        lift.lift_agent(WHO, "watch", tmp_path, now=5000.0)
        assert lift.agent_lifted(SLUG, ME, "watch", tmp_path, now=5000.0 + lift.LIFT_SECONDS - 1)
        assert [r[1] for r in rows(tmp_path)] == ["lift", "lift"]


class TestEntryHonoursAgentLift:
    def run(self, gate, home):
        payload = {"tool_name": "Bash", "tool_input": {"command": "x"}, "session_id": SID}
        env = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": ME, "AGENTIHOOKS_SWARM_TASK": TASK}
        with patch.dict(entry.GATES, {gate.name: gate}), patch("sys.stderr", io.StringIO()):
            return entry.main([gate.name], io.StringIO(json.dumps(payload)), env, home)

    def test_a_page_lift_lets_the_deny_through_as_observed(self, tmp_path):
        gate = Stub()
        lift.lift_agent(WHO, "stub", tmp_path)
        assert self.run(gate, tmp_path) == 0
        assert rows(tmp_path)[-1] == ("stub", "observe", ME, TASK, entry.LIFTED + gate.decision.reason)

    def test_a_page_lift_for_another_agent_does_not_apply(self, tmp_path):
        lift.lift_agent(Who(name="engineer@2-2", swarm=SLUG), "stub", tmp_path)
        assert self.run(Stub(), tmp_path) == 2


class TestTaskLift:
    def run(self, home, head, agent=SUCCESSOR, task=TASK):
        payload = {"tool_name": "Bash", "tool_input": {"command": "x"}, "session_id": SID}
        env = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": agent, "AGENTIHOOKS_SWARM_TASK": task}
        gate = Stub()
        with (
            patch.dict(entry.GATES, {gate.name: gate}),
            patch("scripts.gates.intent.pr_head", return_value=head) as read,
            patch("sys.stderr", io.StringIO()),
        ):
            return entry.main([gate.name], io.StringIO(json.dumps(payload)), env, home), read

    def test_a_successor_on_the_lifted_task_passes_while_the_head_is_unchanged(self, tmp_path):
        lift.lift_agent(WHO, "stub", tmp_path, now=100.0, pull={"url": URL, "head": "a1"})
        code, read = self.run(tmp_path, "a1")
        assert code == 0
        read.assert_called_once_with(URL)
        assert rows(tmp_path)[-1][:3] == ("stub", "observe", SUCCESSOR)

    def test_a_new_push_needs_a_new_lift_for_every_agent_on_the_task(self, tmp_path):
        lift.lift_agent(WHO, "stub", tmp_path, pull={"url": URL, "head": "a1"})
        assert self.run(tmp_path, "b2")[0] == 2
        assert self.run(tmp_path, "b2", agent=ME)[0] == 2
        assert self.run(tmp_path, None)[0] == 2
        lift.lift_agent(WHO, "stub", tmp_path, pull={"url": URL, "head": "b2"})
        assert self.run(tmp_path, "b2")[0] == 0

    @pytest.mark.parametrize(
        "target,allowed", [(URL, True), ("7", True), ("https://github.com/o/r/pull/8", False), ("8", False)]
    )
    def test_a_task_lift_covers_only_its_own_pull_request(self, tmp_path, target, allowed):
        lift.lift_agent(WHO, "stub", tmp_path, pull={"url": URL, "head": "a1"})
        gate = Stub()
        payload = {"tool_name": "Bash", "tool_input": {"command": f"agentihooks swarm {SLUG} merge queue {target}"}}
        env = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": SUCCESSOR, "AGENTIHOOKS_SWARM_TASK": TASK}
        with patch.dict(entry.GATES, {gate.name: gate}), patch("scripts.gates.intent.pr_head", return_value="a1"):
            assert entry.main([gate.name], io.StringIO(json.dumps(payload)), env, tmp_path) == (0 if allowed else 2)

    def test_a_lift_for_another_task_does_not_apply(self, tmp_path):
        lift.lift_agent(Who(name=ME, swarm=SLUG, task="t2"), "stub", tmp_path, pull={"url": URL, "head": "a1"})
        assert self.run(tmp_path, "a1", agent=ME)[0] == 2

    def test_a_task_without_a_pull_request_keeps_the_one_hour_lift(self, tmp_path):
        lift.lift_agent(WHO, "stub", tmp_path, now=100.0)
        assert lift.task_lifted(SLUG, TASK, "stub", tmp_path, now=100.0 + lift.LIFT_SECONDS - 1)
        assert not lift.task_lifted(SLUG, TASK, "stub", tmp_path, now=100.0 + lift.LIFT_SECONDS)
        assert not lift.task_lifted(SLUG, "", "stub", tmp_path, now=101.0)

    def test_a_task_lift_outside_a_swarm_lifts_nothing(self, tmp_path):
        path = lift.task_lift_path("", TASK, "stub", tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"at": 100.0}))
        assert not lift.task_lifted("", TASK, "stub", tmp_path, now=101.0)

    def test_an_agent_without_a_task_keeps_its_agent_lift(self, tmp_path):
        lift.lift_agent(Who(name="master@1-0", swarm=SLUG), "stub", tmp_path)
        assert self.run(tmp_path, None, agent="master@1-0", task="")[0] == 0

    def test_an_agent_lift_from_before_its_claim_does_not_cover_its_task(self, tmp_path):
        lift.lift_agent(Who(name=ME, swarm=SLUG), "stub", tmp_path)
        assert self.run(tmp_path, "a1", agent=ME)[0] == 2

    def test_a_dropped_task_lift_lifts_nothing_even_for_the_lifted_agent(self, tmp_path):
        lift.lift_agent(WHO, "stub", tmp_path, pull={"url": URL, "head": "a1"})
        lift.drop_task(SLUG, TASK, tmp_path)
        assert lift.agent_lifted(SLUG, ME, "stub", tmp_path)
        assert self.run(tmp_path, "a1", agent=ME)[0] == 2
        assert self.run(tmp_path, "a1")[0] == 2

    def test_task_lift_path_sits_under_the_swarm_home_lifts_tasks_folder(self, tmp_path):
        path = lift.task_lift_path(SLUG, TASK, "watch", tmp_path)
        assert path == tmp_path / SLUG / "gates" / "lifts" / "tasks" / TASK / "watch"


class TestActive:
    def row(self, gate, kind, at, agent=ME):
        return {"at": at, "gate": gate, "kind": kind, "agent": agent, "task": TASK, "tool": "Bash", "reason": "r"}

    def test_a_deny_in_the_last_hour_is_an_active_gate_not_yet_lifted(self):
        now = 10 * HOUR_MS
        recent = [self.row("watch", "deny", now - HOUR_MS + 1), self.row("talk", "deny", now - HOUR_MS)]
        assert lift.active(recent, now) == {ME: [{"gate": "watch", "lifted": False}]}

    def test_a_lift_in_the_last_hour_marks_the_gate_lifted(self):
        now = 10 * HOUR_MS
        recent = [self.row("watch", "deny", now - 5), self.row("watch", "lift", now - 4)]
        assert lift.active(recent, now) == {ME: [{"gate": "watch", "lifted": True}]}

    def test_a_deny_after_a_lift_shows_the_gate_unlifted_again(self):
        now = 10 * HOUR_MS
        recent = [self.row("watch", "lift", now - 5), self.row("watch", "deny", now - 4)]
        assert lift.active(recent, now) == {ME: [{"gate": "watch", "lifted": False}]}

    def test_counts_observes_and_nameless_rows_are_not_active(self):
        now = 10 * HOUR_MS
        recent = [
            self.row("watch", "count", now),
            self.row("watch", "observe", now),
            self.row("watch", "deny", now, ""),
        ]
        assert lift.active(recent, now) == {}

    def test_a_row_without_a_time_is_not_active(self):
        row = self.row("watch", "deny", 0)
        del row["at"]
        assert lift.active([row], HOUR_MS) == {}

    def test_gates_list_in_name_order_per_agent(self):
        now = 10 * HOUR_MS
        recent = [self.row("watch", "deny", now), self.row("identity", "deny", now), self.row("watch", "deny", now)]
        assert [g["gate"] for g in lift.active(recent, now)[ME]] == ["identity", "watch"]


class TestStatus:
    def test_each_agent_carries_its_active_gates(self):
        log.append(SLUG, log.Row.of("watch", "deny", WHO))
        report = status.status_report(saved(), SLUG, {"tasks": []}, SimpleNamespace(live_names=lambda: {ME}))
        assert [a["gates"] for a in report["agents"]] == [[{"gate": "watch", "lifted": False}]]

    def test_a_deny_behind_more_than_twenty_rows_still_counts(self):
        log.append(SLUG, log.Row.of("watch", "deny", WHO))
        for _ in range(25):
            log.append(SLUG, log.Row.of("subagents", "count", WHO))
        report = status.status_report(saved(), SLUG, {"tasks": []}, SimpleNamespace(live_names=lambda: {ME}))
        assert [a["gates"] for a in report["agents"]] == [[{"gate": "watch", "lifted": False}]]

    def test_an_agent_with_no_denies_has_no_gates(self):
        report = status.status_report(saved(), SLUG, {"tasks": []}, SimpleNamespace(live_names=lambda: {ME}))
        assert [a["gates"] for a in report["agents"]] == [[]]

    def test_a_lift_behind_three_hundred_later_rows_still_shows_lifted(self):
        log.append(SLUG, log.Row.of("watch", "deny", WHO))
        log.append(SLUG, log.Row.of("watch", "lift", WHO))
        for _ in range(300):
            log.append(SLUG, log.Row.of("subagents", "count", WHO, "Agent", LONG))
        report = status.status_report(saved(), SLUG, {"tasks": []}, SimpleNamespace(live_names=lambda: {ME}))
        assert [a["gates"] for a in report["agents"]] == [[{"gate": "watch", "lifted": True}]]


class TestWindow:
    def fill(self, home, lift_at, later=300):
        log.append(SLUG, log.Row.of("watch", "deny", WHO, now_ms=lift_at - 1), home)
        log.append(SLUG, log.Row.of("watch", "lift", WHO, now_ms=lift_at), home)
        for step in range(later):
            log.append(SLUG, log.Row.of("subagents", "count", WHO, "Agent", LONG, now_ms=lift_at + 1 + step), home)

    def test_the_log_after_the_lift_is_far_larger_than_one_tail_read(self, tmp_path):
        self.fill(tmp_path, HOUR_MS)
        assert log.gate_log_path(SLUG, tmp_path).stat().st_size > 2 * log.TAIL_BYTES

    def test_a_lift_holds_behind_three_hundred_later_rows_until_its_window_ends(self, tmp_path):
        self.fill(tmp_path, HOUR_MS)
        assert lift.current(SLUG, 2 * HOUR_MS - 1, tmp_path) == {ME: [{"gate": "watch", "lifted": True}]}
        assert lift.current(SLUG, 2 * HOUR_MS + 1000, tmp_path) == {}

    def test_stale_timeless_and_text_timed_rows_inside_the_window_do_not_end_the_read(self, tmp_path):
        self.fill(tmp_path, HOUR_MS, later=0)
        stray = ['{"at": 0, "kind": "count"}', '{"kind": "count"}', '{"at": "soon", "kind": "count"}', '{"at": true}']
        with log.gate_log_path(SLUG, tmp_path).open("a") as out:
            for step in range(300):
                row = log.Row.of("subagents", "count", WHO, "Agent", LONG, now_ms=HOUR_MS + 1 + step)
                out.write(json.dumps(row.__dict__) + "\n" + stray[step % len(stray)] + "\n")
        assert lift.current(SLUG, 2 * HOUR_MS - 1, tmp_path) == {ME: [{"gate": "watch", "lifted": True}]}
        assert len(log.since(SLUG, HOUR_MS, tmp_path)) == 301

    def test_a_window_row_that_opens_a_chunk_is_read_and_keeps_the_read_going(self, tmp_path):
        stale = [self.aligned("count", 0)] * self.PER_CHUNK
        self.write(tmp_path, stale * 2 + [self.aligned("lift", HOUR_MS)] + stale[1:], 3 * log.TAIL_BYTES)
        assert [r["kind"] for r in log.since(SLUG, HOUR_MS, tmp_path)] == ["lift"]

    def test_a_chunk_whose_newest_row_opens_the_window_keeps_the_read_going(self, tmp_path):
        older = [self.aligned("lift", HOUR_MS)] * self.PER_CHUNK
        edge = [self.aligned("count", HOUR_MS)] * self.PER_CHUNK
        self.write(tmp_path, older + edge, 2 * log.TAIL_BYTES)
        assert [r["kind"] for r in log.since(SLUG, HOUR_MS, tmp_path)].count("lift") == self.PER_CHUNK

    def test_a_chunk_without_any_timed_row_keeps_the_read_going(self, tmp_path):
        junk = ["{}".ljust(255) + "\n"] * self.PER_CHUNK
        self.write(tmp_path, [self.aligned("lift", HOUR_MS)] * self.PER_CHUNK + junk, 2 * log.TAIL_BYTES)
        assert [r["kind"] for r in log.since(SLUG, HOUR_MS, tmp_path)].count("lift") == self.PER_CHUNK

    def test_a_single_byte_left_before_the_last_chunk_is_still_read(self, tmp_path):
        first = self.aligned("lift", HOUR_MS)
        later = [self.aligned("count", HOUR_MS)] * (self.PER_CHUNK - 1) + ["\n"]
        self.write(tmp_path, [first] + later, log.TAIL_BYTES + 1)
        assert [r["kind"] for r in log.since(SLUG, HOUR_MS, tmp_path)][0] == "lift"

    PER_CHUNK = log.TAIL_BYTES // 256

    def aligned(self, kind, at):
        return json.dumps(log.Row.of("watch", kind, WHO, now_ms=at).__dict__).ljust(255) + "\n"

    def write(self, home, lines, size):
        text = "".join(lines)
        assert len(text) == size
        path = log.gate_log_path(SLUG, home)
        path.parent.mkdir(parents=True)
        path.write_text(text)

    def test_since_returns_every_row_from_the_given_time_on(self, tmp_path):
        self.fill(tmp_path, HOUR_MS)
        found = log.since(SLUG, HOUR_MS, tmp_path)
        assert [r["at"] for r in found] == list(range(HOUR_MS, HOUR_MS + 301))

    def test_since_keeps_the_first_row_of_a_short_log(self, tmp_path):
        self.fill(tmp_path, HOUR_MS, later=0)
        assert [r["kind"] for r in log.since(SLUG, 0, tmp_path)] == ["deny", "lift"]

    def test_since_skips_unreadable_lines_and_non_objects(self, tmp_path):
        self.fill(tmp_path, HOUR_MS, later=0)
        with log.gate_log_path(SLUG, tmp_path).open("a") as out:
            out.write("not json\n[1]\n")
        assert [r["kind"] for r in log.since(SLUG, 0, tmp_path)] == ["deny", "lift"]

    def test_since_drops_a_row_without_a_time_once_the_window_starts_after_zero(self, tmp_path):
        self.fill(tmp_path, HOUR_MS, later=0)
        with log.gate_log_path(SLUG, tmp_path).open("a") as out:
            out.write('{"kind": "deny"}\n')
        assert [r["kind"] for r in log.since(SLUG, 1, tmp_path)] == ["deny", "lift"]

    def test_since_without_a_log_is_empty(self, tmp_path):
        assert log.since(SLUG, 0, tmp_path) == []


class TestCommand:
    @pytest.fixture(autouse=True)
    def read_heads(self, monkeypatch):
        read = []
        store = saved()
        monkeypatch.setattr("scripts.swarm.store.connect", lambda: store)
        ledger = SimpleNamespace(
            task=lambda slug, task: {"id": task, "pr_url": URL} if (slug, task) == (SLUG, TASK) else None
        )
        monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
        monkeypatch.setattr("scripts.gates.intent.pr_head", lambda url: read.append(url) or "a1")
        return read

    def lift(self, store, gate="watch", agent=ME):
        cli.cmd_lift(store, SimpleNamespace(slug=SLUG, agent=agent, gate=gate))

    def test_the_lift_binds_the_task_and_its_pull_request_head(self, monkeypatch, read_heads):
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
        self.lift(saved())
        assert read_heads == [URL]
        assert lift.task_lifted(SLUG, TASK, "watch", now=time.time() + 2 * lift.LIFT_SECONDS)
        monkeypatch.setattr("scripts.gates.intent.pr_head", lambda url: "b2")
        monkeypatch.setattr("scripts.gates.intent.production_unchanged", lambda url, before, after: False)
        assert not lift.task_lifted(SLUG, TASK, "watch")

    def test_an_unreadable_head_refuses_the_lift_and_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
        monkeypatch.setattr("scripts.gates.intent.pr_head", lambda url: None)
        with pytest.raises(SwarmError, match=f"^could not read the head of {URL}; lift again once GitHub answers$"):
            self.lift(saved())
        assert not lift.task_lift_path(SLUG, TASK, "watch").exists()
        assert rows() == []

    def test_the_operator_lifts_a_gate_for_an_agent(self, monkeypatch, capsys):
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
        self.lift(saved())
        assert capsys.readouterr().out == json.dumps({"agent": ME, "gate": "watch", "head": "a1"}) + "\n"
        assert lift.agent_lifted(SLUG, ME, "watch")
        assert rows() == [("watch", "lift", ME, TASK, lift.HEAD_REASON)]

    def test_the_server_gate_talk_can_be_lifted(self, monkeypatch, capsys, read_heads):
        monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
        self.lift(saved(), gate="talk")
        assert lift.agent_lifted(SLUG, ME, "talk")
        assert not lift.task_lift_path(SLUG, TASK, "talk").exists()
        assert read_heads == []
        assert capsys.readouterr().out == json.dumps({"agent": ME, "gate": "talk", "minutes": 60}) + "\n"

    def test_an_agent_cannot_lift_a_gate(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "connect", saved)
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ME)
        assert cli.main([SLUG, "lift", ME, "watch"]) == 1
        assert capsys.readouterr().err == (
            f"swarm: only the operator or the master of swarm {SLUG} uses its swarm controls, and {ME} is neither\n"
        )
        assert rows() == []

    @pytest.mark.parametrize(
        "agent,gate,refusal",
        [("engineer@9-9", "watch", "engineer@9-9 is not in this swarm"), (ME, "bogus", "no gate named bogus")],
    )
    def test_unknown_agent_or_gate_is_refused(self, monkeypatch, agent, gate, refusal):
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
        with pytest.raises(SwarmError, match=f"^{refusal}$"):
            self.lift(saved(), gate=gate, agent=agent)
        assert rows() == []

    def test_the_parser_takes_agent_then_gate(self):
        args = cli.build_parser().parse_args([SLUG, "lift", ME, "watch"])
        assert (args.command, args.agent, args.gate) == ("lift", ME, "watch")


class TestServerRoute:
    def test_page_lift_routes_to_the_swarm_lift_command(self):
        from scripts.swarm_ledger.ledger_server import control_argv

        assert control_argv({"action": "lift", "agent": ME, "gate": "watch-budget"}) == ["lift", ME, "watch-budget"]
        assert control_argv({"action": "lift", "agent": "Eng@X", "gate": "talk"}) == ["lift", "Eng@X", "talk"]

    @pytest.mark.parametrize(
        "body,refusal",
        [
            ({"action": "lift", "agent": "--as", "gate": "watch"}, "A lift needs an agent name"),
            ({"action": "lift", "agent": ME, "gate": "../x"}, "A lift needs the gate's name"),
            ({"action": "lift", "agent": ME}, "A lift needs the gate's name"),
            ({"action": "lift", "gate": "watch"}, "A lift needs an agent name"),
        ],
    )
    def test_bad_lifts_are_refused(self, body, refusal):
        from scripts.swarm_ledger.ledger_server import control_argv

        with pytest.raises(ValueError, match=f"^{refusal}$"):
            control_argv(body)
