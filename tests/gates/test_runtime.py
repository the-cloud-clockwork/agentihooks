"""The gate runtime: modes, the gate log, verdict files, the operator lift and counted fail-open."""

import io
import json
from unittest.mock import patch

import pytest

from scripts.gates import entry, lift, log, modes
from scripts.gates.base import Call, Decision, Who
from scripts.gates.verdicts import Verdicts

SLUG, ME, TASK, SID = "demo", "engineer@1-1", "t1", "sid-1"
WHO = Who(name=ME, swarm=SLUG, task=TASK)
SWARM_ENV = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": ME, "AGENTIHOOKS_SWARM_TASK": TASK}


class Stub:
    def __init__(
        self, name="stub", decision=Decision.deny("too many talks: run swarm progress"), default_mode="enforce"
    ):
        self.name, self.decision, self.default_mode = name, decision, default_mode
        self.states = []

    def matches(self, call):
        return call.tool == "Bash"

    def decide(self, call, who, state):
        self.states.append(state)
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


class TestModes:
    def test_env_name_upper_cases_and_replaces_dashes(self):
        assert modes.env_name("talk-budget") == "AGENTIHOOKS_GATE_TALK_BUDGET"

    @pytest.mark.parametrize("value", ["enforce", "observe", "off", " Observe "])
    def test_env_value_picks_the_mode(self, value):
        assert modes.mode(Stub(), {"AGENTIHOOKS_GATE_STUB": value}) == value.strip().lower()

    @pytest.mark.parametrize("env", [{}, {"AGENTIHOOKS_GATE_STUB": "loud"}, {"AGENTIHOOKS_GATE_STUB": ""}])
    def test_missing_or_unknown_value_keeps_the_gate_default(self, env):
        assert modes.mode(Stub(default_mode="observe"), env) == "observe"

    def test_modes_are_the_three_named(self):
        assert modes.MODES == ("enforce", "observe", "off")


class TestLog:
    def test_path_is_the_one_doctor_rates_reads(self, tmp_path):
        from scripts.doctor import rates_read

        assert log.gate_log_path(SLUG, tmp_path) == tmp_path / SLUG / "gates" / "log.jsonl"
        assert rates_read.gate_log_path is log.gate_log_path

    def test_default_home_is_the_swarm_home(self, tmp_path):
        from pathlib import Path

        assert log.gate_log_path(SLUG) == Path.home() / ".agentihooks" / "swarm" / SLUG / "gates" / "log.jsonl"

    def test_row_carries_who_and_the_call(self):
        row = log.Row.of("talk", "deny", WHO, "Bash", "over budget", now_ms=7)
        assert row == log.Row(at=7, gate="talk", kind="deny", agent=ME, task=TASK, tool="Bash", reason="over budget")

    def test_row_time_defaults_to_now(self):
        with patch("time.time", return_value=12.5):
            assert log.Row.of("talk", "lift", WHO).at == 12500

    def test_append_writes_one_json_line_per_row(self, tmp_path):
        log.append(SLUG, log.Row.of("talk", "deny", WHO, "Bash", "a", now_ms=1), tmp_path)
        log.append(SLUG, log.Row.of("talk", "observe", WHO, "Bash", "b", now_ms=2), tmp_path)
        lines = log.gate_log_path(SLUG, tmp_path).read_text().splitlines()
        assert [json.loads(line) for line in lines] == [
            {"at": 1, "gate": "talk", "kind": "deny", "agent": ME, "task": TASK, "tool": "Bash", "reason": "a"},
            {"at": 2, "gate": "talk", "kind": "observe", "agent": ME, "task": TASK, "tool": "Bash", "reason": "b"},
        ]

    def test_recent_returns_the_last_rows_oldest_first(self, tmp_path):
        for n in range(5):
            log.append(SLUG, log.Row.of("talk", "deny", WHO, now_ms=n), tmp_path)
        assert [r["at"] for r in log.recent(SLUG, 3, tmp_path)] == [2, 3, 4]

    def test_recent_skips_broken_lines_and_reads_a_missing_log_as_empty(self, tmp_path):
        assert log.recent(SLUG, 5, tmp_path) == []
        path = log.gate_log_path(SLUG, tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"at": 1}\nnot json\n[1]\n{"at": 2}\n')
        assert log.recent(SLUG, 5, tmp_path) == [{"at": 1}, {"at": 2}]

    def test_recent_reads_only_the_tail_of_a_long_log(self, tmp_path, monkeypatch):
        for n in range(10):
            log.append(SLUG, log.Row.of("g", "deny", Who(), now_ms=n), tmp_path)
        size = len(log.gate_log_path(SLUG, tmp_path).read_bytes()) // 10
        monkeypatch.setattr(log, "TAIL_BYTES", size * 3 + size // 2)
        assert [r["at"] for r in log.recent(SLUG, 50, tmp_path)] == [7, 8, 9]
        monkeypatch.setattr(log, "TAIL_BYTES", size * 3)
        assert [r["at"] for r in log.recent(SLUG, 50, tmp_path)] == [8, 9]
        monkeypatch.setattr(log, "TAIL_BYTES", size * 10)
        assert [r["at"] for r in log.recent(SLUG, 50, tmp_path)] == list(range(10))

    def test_recent_default_limit_is_twenty(self, tmp_path):
        for n in range(25):
            log.append(SLUG, log.Row.of("g", "deny", Who(), now_ms=n), tmp_path)
        assert len(log.recent(SLUG, home=tmp_path)) == 20


class TestVerdicts:
    def test_write_then_read_round_trips_verdict_and_reason(self, tmp_path):
        store = Verdicts(SLUG, "intent", tmp_path)
        written = store.write("t1", "fail", "no way to reach the feature", now_ms=5)
        assert written == {"verdict": "fail", "reason": "no way to reach the feature", "at": 5}
        assert store.read("t1") == written
        assert store.path("t1") == tmp_path / SLUG / "gates" / "intent" / "t1"

    def test_time_defaults_to_now(self, tmp_path):
        with patch("time.time", return_value=3.0):
            assert Verdicts(SLUG, "intent", tmp_path).write("t1", "pass", "")["at"] == 3000

    def test_missing_or_broken_verdict_reads_none(self, tmp_path):
        store = Verdicts(SLUG, "intent", tmp_path)
        assert store.read("t1") is None
        store.path("t1").parent.mkdir(parents=True)
        store.path("t1").write_text("{")
        assert store.read("t1") is None
        store.path("t1").write_text("[1]")
        assert store.read("t1") is None

    def test_clear_removes_and_tolerates_missing(self, tmp_path):
        store = Verdicts(SLUG, "intent", tmp_path)
        store.write("t1", "fail", "x")
        store.clear("t1")
        store.clear("t1")
        assert store.read("t1") is None

    @pytest.mark.parametrize(
        "subject,name", [("../x", "___x"), ("a/b", "a_b"), ("", "_"), ("engineer@1-1", "engineer@1-1")]
    )
    def test_subject_stays_inside_the_gate_folder(self, tmp_path, subject, name):
        assert Verdicts(SLUG, "intent", tmp_path).path(subject).name == name

    def test_write_replaces_atomically(self, tmp_path):
        store = Verdicts(SLUG, "intent", tmp_path)
        store.write("t1", "fail", "a")
        store.write("t1", "pass", "b")
        assert store.read("t1")["verdict"] == "pass"
        assert [p.name for p in store.path("t1").parent.iterdir()] == ["t1"]

    def test_default_home_is_the_swarm_home(self):
        from pathlib import Path

        assert (
            Verdicts(SLUG, "intent").path("t1")
            == Path.home() / ".agentihooks" / "swarm" / SLUG / "gates" / "intent" / "t1"
        )


class TestLift:
    @pytest.mark.parametrize(
        "prompt,gates",
        [
            ("lift the talk gate", {"talk"}),
            ("please Lift the Talk gate now", {"talk"}),
            ("lift talk gate", {"talk"}),
            ("lift the watch-budget gate and lift the identity gate", {"watch-budget", "identity"}),
            ("the talk gate is fine", set()),
            ("lift the talk", set()),
            ("uplift the talk gate", set()),
            ("lift the talk gates", set()),
            ("", set()),
        ],
    )
    def test_requested_names_each_lifted_gate(self, prompt, gates):
        assert lift.requested(prompt) == gates

    def test_arm_lifts_for_one_hour_for_that_session_only(self, tmp_path):
        lift.arm(SLUG, SID, "talk", tmp_path, now=100.0)
        assert lift.lifted(SLUG, SID, "talk", tmp_path, now=100.0 + lift.LIFT_SECONDS - 1)
        assert not lift.lifted(SLUG, SID, "talk", tmp_path, now=100.0 + lift.LIFT_SECONDS)
        assert not lift.lifted(SLUG, "other", "talk", tmp_path, now=101.0)
        assert not lift.lifted(SLUG, SID, "watch", tmp_path, now=101.0)

    def test_lift_seconds_is_one_hour(self):
        assert lift.LIFT_SECONDS == 3600

    def test_time_defaults_to_now(self, tmp_path):
        with patch("time.time", return_value=50.0):
            lift.arm(SLUG, SID, "talk", tmp_path)
            assert lift.lifted(SLUG, SID, "talk", tmp_path)

    def test_unarmed_broken_or_sessionless_reads_not_lifted(self, tmp_path):
        assert not lift.lifted(SLUG, SID, "talk", tmp_path, now=1.0)
        path = lift.lift_path(SLUG, SID, "talk", tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{")
        assert not lift.lifted(SLUG, SID, "talk", tmp_path, now=1.0)
        for record in ("[]", "{}", '{"at": "1"}', '{"at": null}'):
            path.write_text(record)
            assert not lift.lifted(SLUG, SID, "talk", tmp_path, now=1.0), record
        lift.arm(SLUG, "", "talk", tmp_path, now=1.0)
        assert not lift.lifted(SLUG, "", "talk", tmp_path, now=1.0)
        assert not lift.lifted("", SID, "talk", tmp_path, now=1.0)

    def test_lift_path_keeps_session_and_gate_inside_the_lifts_folder(self, tmp_path):
        assert lift.lift_path(SLUG, "../s", "talk", tmp_path) == tmp_path / SLUG / "gates" / "lifts" / "___s" / "talk"

    def test_arm_from_prompt_arms_logs_and_posts_known_gates(self, tmp_path):
        posted = []
        armed = lift.arm_from_prompt(
            "lift the talk gate and lift the bogus gate", SID, WHO, {"talk", "watch"}, tmp_path, posted.append
        )
        assert armed == ["talk"]
        assert lift.lifted(SLUG, SID, "talk", tmp_path)
        assert not lift.lifted(SLUG, SID, "bogus", tmp_path)
        rows = log.recent(SLUG, home=tmp_path)
        assert [(r["gate"], r["kind"], r["agent"], r["task"]) for r in rows] == [("talk", "lift", ME, TASK)]
        assert rows[0]["reason"] == lift.LIFT_REASON
        assert posted == [{"op": "gate_lift", "id": posted[0]["id"], "by": ME, "gate": "talk"}]
        prefix, _, suffix = posted[0]["id"].partition("-")
        assert prefix == "gate_lift" and len(suffix) == 10 and int(suffix, 16) >= 0

    def test_arm_from_prompt_posts_to_its_swarms_ledger_by_default(self, tmp_path, monkeypatch):
        posted = []
        monkeypatch.setattr(lift, "post", lambda slug: lambda op: posted.append((slug, op["gate"])))
        assert lift.arm_from_prompt("lift the talk gate", SID, WHO, {"talk"}, tmp_path) == ["talk"]
        assert posted == [(SLUG, "talk")]

    def test_arm_from_prompt_posts_after_every_lift_is_armed(self, tmp_path):
        def post(op):
            raise OSError("ledger down")

        with pytest.raises(OSError):
            lift.arm_from_prompt("lift the a gate, lift the b gate", SID, WHO, {"a", "b"}, tmp_path, post)
        assert lift.lifted(SLUG, SID, "a", tmp_path) and lift.lifted(SLUG, SID, "b", tmp_path)

    @pytest.mark.parametrize("who,sid", [(Who(name=ME), SID), (Who(swarm=SLUG), SID), (WHO, "")])
    def test_arm_from_prompt_needs_a_pinned_swarm_session(self, tmp_path, who, sid):
        posted = []
        assert lift.arm_from_prompt("lift the talk gate", sid, who, {"talk"}, tmp_path, posted.append) == []
        assert posted == [] and log.recent(SLUG, home=tmp_path) == []

    def test_post_sends_through_the_ledger_without_starting_a_server(self):
        sent = []

        class Ledger:
            @staticmethod
            def request(slug, ops):
                sent.append((slug, ops))

        op = {"op": "gate_lift", "id": "gate_lift-1", "by": ME, "gate": "talk"}
        with patch("scripts.swarm.ledger_client._ledger", return_value=Ledger):
            lift.post(SLUG)(op)
        assert sent == [(SLUG, [op])]


class TestEntry:
    def run(self, gate, payload=None, env=SWARM_ENV, home=None):
        payload = (
            {"tool_name": "Bash", "tool_input": {"command": "x"}, "session_id": SID} if payload is None else payload
        )
        err = io.StringIO()
        with patch.dict(entry.GATES, {gate.name: gate}), patch("sys.stderr", err):
            code = entry.main([gate.name], io.StringIO(json.dumps(payload)), env, home)
        return code, err.getvalue()

    def rows(self, home):
        return [
            (r["gate"], r["kind"], r["agent"], r["task"], r["tool"], r["reason"]) for r in log.recent(SLUG, home=home)
        ]

    def test_enforce_denies_and_logs_a_deny_row(self, tmp_path):
        gate = Stub()
        assert self.run(gate, home=tmp_path) == (2, gate.decision.reason + "\n")
        assert self.rows(tmp_path) == [("stub", "deny", ME, TASK, "Bash", gate.decision.reason)]

    def test_observe_lets_the_call_through_silently_and_logs_it(self, tmp_path):
        gate = Stub(default_mode="observe")
        assert self.run(gate, home=tmp_path) == (0, "")
        assert self.rows(tmp_path) == [("stub", "observe", ME, TASK, "Bash", gate.decision.reason)]

    def test_env_mode_overrides_the_default(self, tmp_path):
        gate = Stub()
        assert self.run(gate, env={**SWARM_ENV, "AGENTIHOOKS_GATE_STUB": "observe"}, home=tmp_path) == (0, "")
        assert self.rows(tmp_path)[0][1] == "observe"

    def test_off_skips_the_gate(self, tmp_path):
        gate = Stub()
        assert self.run(gate, env={**SWARM_ENV, "AGENTIHOOKS_GATE_STUB": "off"}, home=tmp_path) == (0, "")
        assert gate.states == [] and self.rows(tmp_path) == []

    def test_allow_logs_nothing(self, tmp_path):
        assert self.run(Stub(decision=Decision()), home=tmp_path) == (0, "")
        assert self.rows(tmp_path) == []

    def test_unmatched_call_is_not_decided(self, tmp_path):
        gate = Stub()
        assert self.run(gate, {"tool_name": "Read", "tool_input": {}}, home=tmp_path) == (0, "")
        assert gate.states == []

    def test_a_raising_gate_fails_open_and_is_counted(self, tmp_path):
        gate = Stub(decision=KeyError("verdict"))
        assert self.run(gate, home=tmp_path) == (0, "")
        assert self.rows(tmp_path) == [("stub", "fail-open", ME, TASK, "Bash", "KeyError: 'verdict'")]

    def test_a_lift_lets_a_deny_through_and_logs_it_observed(self, tmp_path):
        gate = Stub()
        lift.arm(SLUG, SID, "stub", tmp_path)
        assert self.run(gate, home=tmp_path) == (0, "")
        assert self.rows(tmp_path) == [
            ("stub", "observe", ME, TASK, "Bash", "lifted by the operator: " + gate.decision.reason)
        ]

    def test_a_lift_for_another_session_does_not_apply(self, tmp_path):
        lift.arm(SLUG, "sid-2", "stub", tmp_path)
        assert self.run(Stub(), home=tmp_path)[0] == 2

    def test_decide_gets_the_verdicts_of_its_swarm_and_gate(self, tmp_path):
        gate = Stub(decision=Decision())
        self.run(gate, home=tmp_path)
        (state,) = gate.states
        assert isinstance(state, Verdicts)
        assert state.path("t1") == tmp_path / SLUG / "gates" / "stub" / "t1"

    def test_outside_a_swarm_a_deny_still_denies_and_logs_nothing(self, tmp_path):
        home = tmp_path / "swarm"
        assert self.run(Stub(), env={"AGENTIHOOKS_AGENT_NAME": ME}, home=home)[0] == 2
        assert not home.exists()

    def test_outside_a_swarm_a_raising_gate_still_fails_open(self, tmp_path):
        home = tmp_path / "swarm"
        assert self.run(Stub(decision=ValueError("x")), env={}, home=home) == (0, "")
        assert not home.exists()

    def test_identity_ships_enforcing(self):
        assert entry.GATES["identity"].default_mode == "enforce"


def test_gate_protocol_names_the_default_mode():
    from scripts.gates.base import Gate

    assert "default_mode" in Gate.__annotations__
    assert isinstance(Stub(), Gate)


def test_call_tool_used_in_rows():
    assert Call.from_payload({"tool_name": "Edit"}).tool == "Edit"
