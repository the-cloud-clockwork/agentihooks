"""The operator's page lift: a swarm command arms a gate lift for one agent and logs it like the typed lift."""

import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.gates import entry, lift, log
from scripts.gates.base import Who
from scripts.swarm import cli, status
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.gates.test_runtime import SID, Stub

pytestmark = pytest.mark.xdist_group("fakeredis")
SLUG, ME, TASK = "sw", "engineer@1-1", "t1"
WHO = Who(name=ME, swarm=SLUG, task=TASK)
HOUR_MS = lift.LIFT_SECONDS * 1000


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
        report = status.status_report(saved(), SLUG, {"tasks": []})
        assert [a["gates"] for a in report["agents"]] == [[{"gate": "watch", "lifted": False}]]

    def test_a_deny_behind_more_than_twenty_rows_still_counts(self):
        log.append(SLUG, log.Row.of("watch", "deny", WHO))
        for _ in range(25):
            log.append(SLUG, log.Row.of("subagents", "count", WHO))
        report = status.status_report(saved(), SLUG, {"tasks": []})
        assert [a["gates"] for a in report["agents"]] == [[{"gate": "watch", "lifted": False}]]

    def test_an_agent_with_no_denies_has_no_gates(self):
        report = status.status_report(saved(), SLUG, {"tasks": []})
        assert [a["gates"] for a in report["agents"]] == [[]]


class TestCommand:
    def lift(self, store, gate="watch", agent=ME):
        cli.cmd_lift(store, SimpleNamespace(slug=SLUG, agent=agent, gate=gate))

    def test_the_operator_lifts_a_gate_for_an_agent(self, monkeypatch, capsys):
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
        self.lift(saved())
        assert capsys.readouterr().out == json.dumps({"agent": ME, "gate": "watch", "minutes": 60}) + "\n"
        assert lift.agent_lifted(SLUG, ME, "watch")
        assert rows() == [("watch", "lift", ME, TASK, lift.LIFT_REASON)]

    def test_the_server_gate_talk_can_be_lifted(self, monkeypatch):
        monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
        self.lift(saved(), gate="talk")
        assert lift.agent_lifted(SLUG, ME, "talk")

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
