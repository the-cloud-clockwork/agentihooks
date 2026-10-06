"""Gate modes come from the swarm config the operator sets; the agent environment is the fallback outside a swarm."""

import io
import json

import pytest

from scripts.gates import catalog, entry, intent, log, modes, talk
from scripts.gates.base import Decision
from scripts.gates.claims import GATE as CLAIMS
from scripts.swarm import cli, status
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm.trace_plan import GATE as TRACE_PLAN
from scripts.swarm_ledger import ledger_server
from tests.gates.test_runtime import SLUG, SWARM_ENV, Stub
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")
STUB_ENV = "AGENTIHOOKS_GATE_STUB"


def rows(home):
    return log.recent(SLUG, home=home)


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("demo", "/repo", max_eng=1, max_ci=0))
    return s


class TestModeReader:
    @pytest.mark.parametrize("chosen", ["enforce", "observe", "off"])
    def test_the_swarm_config_entry_wins_over_the_environment(self, chosen):
        environ = {STUB_ENV: "enforce" if chosen != "enforce" else "off"}
        assert modes.mode(Stub(), environ, {"stub": chosen}) == chosen

    def test_inside_a_swarm_an_unset_entry_keeps_the_default_whatever_the_environment_says(self):
        assert modes.mode(Stub(default_mode="observe"), {STUB_ENV: "off"}, {"other": "off"}) == "observe"

    def test_an_unknown_config_value_keeps_the_default(self):
        assert modes.mode(Stub(), {}, {"stub": "loud"}) == "enforce"

    @pytest.mark.parametrize(
        ("gates", "chosen"), [({"stub": " Observe "}, "observe"), ({}, "enforce"), ({"stub": 1}, "enforce")]
    )
    def test_configured_reads_only_the_swarm_config(self, gates, chosen):
        assert modes.configured(Stub(), gates) == chosen

    def test_outside_a_swarm_the_environment_still_picks_the_mode(self):
        assert modes.mode(Stub(), {STUB_ENV: "observe"}, None) == "observe"
        assert modes.mode(Stub(), {STUB_ENV: "observe"}) == "observe"


class TestSwarmGates:
    def test_no_swarm_reads_none(self):
        assert modes.swarm_gates("", {}) is None

    def test_reads_the_named_swarm_config_from_the_redis_the_environment_names(self, store, monkeypatch):
        store.update("demo", gates={"watch": "off"})
        seen, environ = [], {"AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:6379/12"}
        monkeypatch.setattr(
            "scripts.swarm.store.redis_client", lambda environ=None: seen.append(environ) or store.redis
        )
        assert modes.swarm_gates("demo", environ) == {"watch": "off"}
        assert seen == [environ]

    @pytest.mark.parametrize("slug", ["demo", "gone"])
    def test_an_unreadable_config_falls_back_to_the_environment(self, store, monkeypatch, slug):
        import redis

        def unreachable(environ=None):
            raise redis.ConnectionError("redis down")

        monkeypatch.setattr(
            "scripts.swarm.store.redis_client", unreachable if slug == "demo" else lambda e=None: store.redis
        )
        assert modes.swarm_gates(slug, {}) is None


class TestEntry:
    def run(self, gate, environ, gates, tmp_path, monkeypatch):
        monkeypatch.setitem(entry.GATES, gate.name, gate)
        reads = []
        monkeypatch.setattr(modes, "swarm_gates", lambda swarm, given: reads.append((swarm, given)) or gates)
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "sid-1"}
        code = entry.main([gate.name], io.StringIO(json.dumps(payload)), environ, tmp_path)
        assert all(given is environ for _, given in reads)
        return code, [swarm for swarm, _ in reads]

    def test_observe_in_the_swarm_config_logs_the_deny_although_the_agent_environment_says_enforce(
        self, tmp_path, monkeypatch
    ):
        environ = {**SWARM_ENV, STUB_ENV: "enforce"}
        code, seen = self.run(Stub(), environ, {"stub": "observe"}, tmp_path, monkeypatch)
        assert (code, seen) == (0, ["demo"])
        assert [r["kind"] for r in rows(tmp_path)] == ["observe"]

    def test_enforce_in_the_swarm_config_denies_although_the_agent_environment_says_off(self, tmp_path, monkeypatch):
        environ = {**SWARM_ENV, STUB_ENV: "off"}
        code, _ = self.run(Stub(), environ, {"stub": "enforce"}, tmp_path, monkeypatch)
        assert code == 2
        assert [r["kind"] for r in rows(tmp_path)] == ["deny"]

    def test_off_in_the_swarm_config_skips_the_gate(self, tmp_path, monkeypatch):
        gate = Stub()
        code, _ = self.run(gate, SWARM_ENV, {"stub": "off"}, tmp_path, monkeypatch)
        assert (code, gate.states, rows(tmp_path)) == (0, [], [])

    def test_a_call_the_gate_does_not_match_never_reads_the_swarm_config(self, tmp_path, monkeypatch):
        gate = Stub()
        gate.matches = lambda call: False
        code, seen = self.run(gate, SWARM_ENV, {"stub": "enforce"}, tmp_path, monkeypatch)
        assert (code, seen) == (0, [])

    def test_an_allowed_call_writes_no_row(self, tmp_path, monkeypatch):
        code, _ = self.run(Stub(decision=Decision()), SWARM_ENV, {"stub": "enforce"}, tmp_path, monkeypatch)
        assert (code, rows(tmp_path)) == (0, [])


class TestCatalog:
    def test_every_gate_is_named_with_its_default(self):
        expected = {name: gate.default_mode for name, gate in entry.GATES.items()}
        expected |= {CLAIMS.name: CLAIMS.default_mode, TRACE_PLAN.name: TRACE_PLAN.default_mode}
        expected |= {talk.NAME: talk.DEFAULT_MODE}
        assert catalog.defaults() == expected
        assert {intent.NAME, talk.NAME, "claims", "trace-plan", "identity", "build"} <= set(catalog.defaults())

    def test_current_fills_unset_and_unknown_entries_with_the_default(self):
        current = catalog.current({"talk": "enforce", "claims": "loud", "nope": "off"})
        assert current == {**catalog.defaults(), "talk": "enforce"}

    def test_status_reports_the_current_mode_of_every_gate(self, store):
        store.update("demo", gates={"watch": "observe"})
        report = status.status_report(store, "demo", {"tasks": [], "_meta": {"events": []}})
        assert report["gate_modes"] == {
            name: modes.label(mode) for name, mode in {**catalog.defaults(), "watch": "observe"}.items()
        }


class TestSwarmSet:
    @pytest.mark.parametrize("name", list(catalog.defaults()))
    def test_the_operator_sets_every_gate_mode(self, env, monkeypatch, name):  # noqa: F811
        store, _, _ = env
        monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
        run("sw", "create", "--repo", "/repo")
        assert run("sw", "set", f"{name}-gate=observe") == 0
        assert store.config("sw").gates == {name: "observe"}

    @pytest.mark.parametrize("name", ["claims", "identity", "trace-plan"])
    def test_an_agent_write_of_any_gate_mode_is_refused(self, env, monkeypatch, capsys, name):  # noqa: F811
        store, _, _ = env
        monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
        run("sw", "create", "--repo", "/repo")
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@1-1")
        assert run("sw", "set", f"{name}-gate=off") == 1
        assert f"only the operator sets {name}-gate" in capsys.readouterr().err
        assert store.config("sw").gates == {}

    def test_an_unknown_gate_key_is_refused(self):
        assert "nope-gate" not in cli.GATE_KEYS
        with pytest.raises(SwarmError, match="watch-gate takes deny"):
            cli.gate_mode("watch-gate", "loud", {})


class TestServerControl:
    def test_the_page_sends_gate_modes_as_swarm_set_pairs(self):
        argv = ledger_server.control_argv({"action": "set", "gates": {"talk": "enforce", "claims": "observe"}})
        assert argv == ["set", "talk-gate=enforce", "claims-gate=observe"]

    def test_gate_modes_ride_with_the_other_settings_in_one_set(self):
        argv = ledger_server.control_argv({"action": "set", "autonomy": "full", "gates": {"talk": "off"}})
        assert argv == ["set", "autonomy=full", "talk-gate=off"]

    @pytest.mark.parametrize(
        "gates", [{"nope": "off"}, {"talk": "loud"}, ["talk"], {"talk": None}, {"-x": "off"}, {}], ids=str
    )
    def test_an_unknown_gate_or_mode_is_refused(self, gates):
        refusal = f"gates maps a gate of {', '.join(catalog.defaults())} to deny, log only, skip"
        with pytest.raises(ValueError) as caught:
            ledger_server.control_argv({"action": "set", "gates": gates})
        assert str(caught.value) == refusal


@pytest.mark.parametrize("mode", ["deny", "log only", "skip"])
def test_page_gate_control_accepts_new_mode_names(mode):
    assert ledger_server.control_argv({"action": "set", "gates": {"watch": mode}}) == ["set", f"watch-gate={mode}"]


@pytest.mark.parametrize(
    "stored,display",
    [("enforce", "deny"), ("observe", "log only"), ("off", "skip"), ("lift", "lift"), ("fail-open", "fail-open")],
)
def test_gate_decision_labels_keep_non_mode_decisions(stored, display):
    assert modes.label(stored) == display
