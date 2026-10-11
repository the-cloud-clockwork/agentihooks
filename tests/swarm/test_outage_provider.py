from dataclasses import replace

import pytest

from scripts.swarm import outage, pane, provider_probe
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.mark.parametrize(
    ("harness", "text", "match"),
    [
        ("claude", "\x1b[31mAPI Error: 529\x1b[0m overloaded", "API Error: 529"),
        ("codex", "stream disconnected before completion: transport error", "stream disconnected before completion"),
        ("copilot", "Request failed with status code 503", "Request failed with status code 503"),
        ("claude", "Unable to connect to API", "Unable to connect to API"),
        ("claude", "Connection error.", "Connection error."),
        ("claude", "api error: 500", "api error: 500"),
        ("codex", "unexpected status 503 Service Unavailable", "unexpected status 503"),
        ("codex", "error sending request for url", "error sending request for url"),
        ("copilot", "Failed to connect to model provider", "Failed to connect to model provider"),
        ("claude", "API Error: 401 Unauthorized", ""),
        ("codex", "unexpected status 429 Too Many Requests", ""),
        ("unknown", "API Error: 529", ""),
    ],
)
def test_provider_errors_are_matched_only_for_their_harness(harness, text, match):
    assert pane.provider_error(harness, text) == match


def test_runtime_carries_provider_evidence_until_an_affirmative_probe_succeeds(tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.setattr(outage, "HOME", tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="claude", account="alpha", model="model")
    store.put_agent("sw", agent)
    text = "API Error: 503 Service Unavailable"

    def herdr(args):
        if args[1] == "get":
            return {"name": "worker", "pane_id": "p1", "agent_status": "idle"}
        return {"text": text}

    runtime = HerdrRuntime(herdr=herdr)
    observed = runtime.observe(agent)
    assert observed.provider_error == "API Error: 503"
    assert observed.provider_checked
    outage.providers(store, "sw", runtime, 1000)
    assert outage.open_records("sw")[0]["evidence"] == ["worker (claude, account alpha): API Error: 503"]
    monkeypatch.setattr(provider_probe, "check", lambda *args: provider_probe.Result(True, "request succeeded"))
    outage.providers(store, "sw", runtime, 61000)
    assert outage.read("sw")["provider"]["ended_at"] == 61000
    assert set(outage.read("sw")["provider"]["affected_agents"]) == {"worker"}
    outage.providers(store, "sw", runtime, 62000)
    assert outage.read("sw")["provider"]["started_at"] == 1000
    assert outage.read("sw")["provider"]["ended_at"] == 61000


def test_an_unreadable_or_remote_pane_cannot_clear_a_provider_outage(tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.setattr(outage, "HOME", tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1")
    store.put_agent("sw", agent)
    outage.observe("sw", "provider", ["worker: connection error"], 1000)

    def herdr(args):
        if args[1] == "get":
            return {"name": "worker", "pane_id": "p1", "agent_status": "idle"}
        raise OSError("pane unavailable")

    runtime = HerdrRuntime(herdr=herdr)
    assert not runtime.observe(agent).provider_checked
    assert not runtime.observe(replace(agent, runtime_backend="kubernetes")).provider_checked
    outage.providers(store, "sw", runtime, 2000)
    assert outage.read("sw")["provider"]["ended_at"] is None


@pytest.mark.parametrize("finished", [False, True])
def test_pane_changes_never_close_provider_records_without_a_successful_probe(tmp_path, monkeypatch, finished):
    import fakeredis

    monkeypatch.setattr(outage, "HOME", tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    affected = AgentRecord("affected", "eng", "t1", harness="claude", account="alpha", model="model")
    unrelated = AgentRecord("unrelated", "eng", "t2", harness="claude")
    store.put_agent("sw", affected)
    store.put_agent("sw", unrelated)
    runtime = HerdrRuntime()
    observations = {
        "affected": pane.PaneObservation("idle", provider_error="API Error: 503", provider_checked=True),
        "unrelated": pane.PaneObservation("idle", provider_checked=True),
    }
    monkeypatch.setattr(runtime, "observe", lambda agent: observations[agent.name])
    outage.providers(store, "sw", runtime, 1000)
    if finished:
        store.put_agent("sw", replace(affected, state="finished"))
    else:
        observations["affected"] = pane.PaneObservation("idle", provider_checked=True)
        observations["unrelated"] = pane.PaneObservation("unknown")
    outage.providers(store, "sw", runtime, 2000)
    assert outage.read("sw")["provider"]["ended_at"] is None
    monkeypatch.setattr(provider_probe, "check", lambda *args: provider_probe.Result(True, "request succeeded"))
    outage.providers(store, "sw", runtime, 61000)
    assert outage.read("sw")["provider"]["ended_at"] == 61000


def test_each_affected_account_is_probed_once_and_the_last_success_closes_the_episode(tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.setattr(outage, "HOME", tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    for name, harness, account in [("a", "claude", "alpha"), ("b", "claude", "alpha"), ("c", "codex", "beta")]:
        store.put_agent("sw", AgentRecord(name, "eng", name, harness=harness, account=account, model="model"))
    runtime = HerdrRuntime()
    monkeypatch.setattr(
        runtime,
        "observe",
        lambda agent: pane.PaneObservation("idle", provider_error="connection failed", provider_checked=True),
    )
    calls = []

    def probe(target, directory):
        calls.append((target.harness, target.account))
        return provider_probe.Result(target.account == "alpha", "request outcome")

    monkeypatch.setattr(provider_probe, "check", probe)
    outage.providers(store, "sw", runtime, 1000)
    outage.providers(store, "sw", runtime, 61000)
    assert sorted(calls) == [("claude", "alpha"), ("codex", "beta")]
    assert outage.read("sw")["provider"]["ended_at"] is None
    monkeypatch.setattr(provider_probe, "check", lambda target, directory: provider_probe.Result(True, "succeeded"))
    outage.providers(store, "sw", runtime, 181000)
    record = outage.read("sw")["provider"]
    assert record["started_at"] == 1000
    assert record["ended_at"] == 181000
    assert set(record["affected_agents"]) == {"a", "b", "c"}
    assert record["probes"]["claude:alpha"]["succeeded_at"] == 61000
    assert record["probes"]["codex:beta"]["succeeded_at"] == 181000
