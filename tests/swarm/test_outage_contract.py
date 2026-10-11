import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.swarm import outage, pane, provider_probe


def test_default_state_path_survives_missing_redis(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    outage.redis_state(["unreachable"], 1000)
    directory = tmp_path / ".agentihooks" / "swarm"
    assert outage.home() == directory
    assert outage.read("_controller")["redis"] == {
        "kind": "redis",
        "started_at": 1000,
        "evidence": ["unreachable"],
        "ended_at": None,
    }
    assert (directory / "_controller" / "outages.json").is_file()


def test_status_output_preserves_all_evidence_and_empty_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    assert outage.show("sw", False) is False
    assert capsys.readouterr().out == ""
    outage.observe("sw", "redis", ["connection failed", "ping failed"], 1000)
    outage.observe("sw", "ledger", ["health failed"], 2000)
    assert outage.show("sw", False) is True
    assert capsys.readouterr().out == (
        "outage redis since 1000: connection failed ; ping failed\noutage ledger since 2000: health failed\n"
    )
    assert outage.show("sw", True) is True
    assert json.loads(capsys.readouterr().out) == {"outages": outage.open_records("sw")}


@pytest.mark.parametrize("error", [OSError("unavailable"), SystemExit(1)])
def test_default_ledger_probe_records_failure_then_affirmative_recovery(tmp_path, monkeypatch, error):
    from scripts.swarm_ledger import ledger_link

    monkeypatch.setattr(outage, "HOME", tmp_path)
    monkeypatch.setattr(ledger_link, "serving", MagicMock(side_effect=error))
    assert outage.ledger_health("sw", 1000) is False
    assert outage.read("sw")["ledger"]["evidence"] == ["Ledger health probe failed"]
    monkeypatch.setattr(ledger_link, "serving", lambda: True)
    assert outage.ledger_health("sw", 2000) is True
    assert outage.read("sw")["ledger"]["ended_at"] == 2000


def test_redis_failure_evidence_and_recovery_timestamp(tmp_path, monkeypatch, capsys):
    from scripts.swarm.store import SwarmError

    monkeypatch.setattr(outage, "HOME", tmp_path)
    (tmp_path / "sw").mkdir()
    assert outage.connect_tick(MagicMock(side_effect=SwarmError("unreachable")), lambda: 1000) is None
    assert capsys.readouterr().out == "Redis connect check failed: SwarmError\n"
    assert outage.read("sw")["redis"]["evidence"] == ["Redis connect check failed: SwarmError"]
    store = object()
    assert outage.connect_tick(lambda: store, lambda: 2000) is store
    assert outage.read("sw")["redis"]["ended_at"] == 2000


def actor(name, *, state="active", account="alpha"):
    return SimpleNamespace(name=name, state=state, harness="claude", account=account, model="chosen-model")


def observed(error="API Error: 503", fingerprint="episode-one", checked=True):
    return pane.PaneObservation("idle", provider_error=error, provider_checked=checked, provider_error_id=fingerprint)


def test_provider_episode_preserves_actor_metadata_and_pending_account_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    agents = [actor("one")]
    observations = {"one": observed()}
    store = SimpleNamespace(agents=lambda slug: agents)
    runtime = SimpleNamespace(observe=lambda agent: observations[agent.name])
    calls = []

    def probe(target, directory):
        calls.append((target, directory))
        return provider_probe.Result(False, "unavailable", 62000)

    monkeypatch.setattr(provider_probe, "check", probe)
    outage.providers(store, "sw", runtime, 1000)
    outage.providers(store, "sw", runtime, 61000)
    agents.append(actor("two"))
    observations["two"] = observed(fingerprint="episode-two")
    outage.providers(store, "sw", runtime, 70000)
    record = outage.read("sw")["provider"]
    assert record["kind"] == "provider"
    assert record["started_at"] == 1000
    assert record["ended_at"] is None
    assert record["evidence"] == [
        "one (claude, account alpha): API Error: 503",
        "two (claude, account alpha): API Error: 503",
    ]
    assert record["affected_agents"] == {
        name: {
            "harness": "claude",
            "account": "alpha",
            "model": "chosen-model",
            "error_id": fingerprint,
            "evidence": f"{name} (claude, account alpha): API Error: 503",
        }
        for name, fingerprint in [("one", "episode-one"), ("two", "episode-two")]
    }
    assert record["observed_errors"] == {"one": "episode-one", "two": "episode-two"}
    assert record["probes"]["claude:alpha"]["next_at"] == 182000
    assert record["probes"]["claude:alpha"]["evidence"] == "unavailable"
    assert calls == [(provider_probe.Target("claude", "alpha", "chosen-model"), tmp_path / "sw" / "provider-probes")]


def test_closed_provider_episode_ignores_unreadable_and_finished_agents_then_recognizes_new_errors(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    agents = [actor("unreadable"), actor("healthy"), actor("affected"), actor("finished", state="finished")]
    observations = {
        "unreadable": observed("", "", False),
        "healthy": observed("", ""),
        "affected": observed(),
        "finished": observed(fingerprint="finished-error"),
    }
    observer = MagicMock(side_effect=lambda agent: observations[agent.name])
    store = SimpleNamespace(agents=lambda slug: agents)
    runtime = SimpleNamespace(observe=observer)
    check = MagicMock(return_value=provider_probe.Result(True, "confirmed", 62000))
    monkeypatch.setattr(provider_probe, "check", check)
    outage.providers(store, "sw", runtime, 1000)
    outage.providers(store, "sw", runtime, 61000)
    record = outage.read("sw")["provider"]
    assert record["ended_at"] == 62000
    assert set(record["affected_agents"]) == {"affected"}
    assert all(call.args[0].state != "finished" for call in observer.call_args_list)
    observations["affected"] = observed("", "", False)
    outage.providers(store, "sw", runtime, 63000)
    assert outage.read("sw")["provider"]["observed_errors"] == {"affected": "episode-one"}
    observations["affected"] = observed()
    outage.providers(store, "sw", runtime, 64000)
    assert outage.read("sw")["provider"]["ended_at"] == 62000
    check.assert_called_once()
    observations["affected"] = observed("", "")
    outage.providers(store, "sw", runtime, 65000)
    assert outage.read("sw")["provider"]["observed_errors"] == {}
    observations["affected"] = observed()
    outage.providers(store, "sw", runtime, 66000)
    assert outage.read("sw")["provider"]["started_at"] == 66000
    assert outage.read("sw")["provider"]["ended_at"] is None


def test_provider_fingerprint_uses_only_normalized_error_lines():
    raw = "  API Error: 503 unavailable  \nConnection error."
    colored = "healthy output\n  \x1b[31mAPI Error: 503 unavailable\x1b[0m  \nConnection error.\n> input"
    expected = hashlib.sha256(b"API Error: 503 unavailable\nConnection error.").hexdigest()
    assert pane.provider_error_id("claude", raw) == expected
    assert pane.provider_error_id("claude", colored) == expected
    assert pane.provider_error_id("codex", raw) == ""
    assert pane.provider_error_id("claude", "healthy output") == ""


@pytest.mark.parametrize("state", ["idle", "blocked"])
def test_runtime_carries_checked_error_identity_even_when_waiting(state):
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord("worker", "eng", "task", pane_id="p1", harness="claude")
    text = "API Error: 503 unavailable"

    def herdr(args):
        if args[1] == "get":
            return {"name": "worker", "pane_id": "p1", "agent_status": state}
        return {"text": text}

    result = HerdrRuntime(herdr=herdr).observe(agent)
    assert result.state == ("waiting" if state == "blocked" else "idle")
    assert result.provider_checked is True
    assert result.provider_error == "API Error: 503"
    assert result.provider_error_id == hashlib.sha256(text.encode()).hexdigest()


def test_a_fresh_error_rearms_a_recovered_account_in_an_open_episode(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    agents = [actor("one"), actor("two", account="beta")]
    observations = {agent.name: observed() for agent in agents}
    store = SimpleNamespace(agents=lambda slug: agents)
    runtime = SimpleNamespace(observe=lambda agent: observations[agent.name])
    monkeypatch.setattr(
        provider_probe, "check", lambda target, directory: provider_probe.Result(target.account == "alpha", "outcome")
    )
    outage.providers(store, "sw", runtime, 1000)
    outage.providers(store, "sw", runtime, 61000)
    observations["one"] = observed(fingerprint="new-error")
    outage.providers(store, "sw", runtime, 62000)
    record = outage.read("sw")["provider"]
    assert set(record) == {"kind", "started_at", "evidence", "ended_at", "affected_agents", "observed_errors", "probes"}
    assert record["started_at"] == 1000
    assert record["probes"]["claude:alpha"]["succeeded_at"] is None
    assert record["probes"]["claude:alpha"]["next_at"] == 122000
    assert record["probes"]["claude:beta"]["next_at"] == 181000


def test_a_closed_provider_record_never_launches_its_pending_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    agent = actor("affected")
    store = SimpleNamespace(agents=lambda slug: [agent])
    runtime = SimpleNamespace(observe=lambda agent: observed())
    check = MagicMock(return_value=provider_probe.Result(True, "confirmed", 62000))
    monkeypatch.setattr(provider_probe, "check", check)
    outage.providers(store, "sw", runtime, 1000)
    outage.observe("sw", "provider", [], 2000)
    closed = outage.read("sw")["provider"]
    outage.providers(store, "sw", runtime, 61000)
    assert outage.read("sw")["provider"] == closed
    check.assert_not_called()
