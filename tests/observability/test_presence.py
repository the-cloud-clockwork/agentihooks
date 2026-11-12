from types import SimpleNamespace

from hooks.observability import otel, presence

ENV = {
    "AGENTIHOOKS_AGENT_NAME": "engineer@1-2",
    "AGENTIHOOKS_SWARM": "rig",
    "AGENTIHOOKS_SWARM_LANE": "eng",
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_HIVE_ID": "hive-003",
    "AH_CC_TOKEN_work": "x",
}


def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(otel, "record_gauge", lambda name, value, attrs: calls.append((name, value, attrs)))
    return calls


def test_an_agent_session_is_marked_seen_with_its_swarm_identity_and_hive(monkeypatch):
    calls = recorded(monkeypatch)
    presence.agent("sess-1", ENV, 1_700_000_000.5)
    assert calls == [
        (
            "agentihooks.presence.agent",
            1_700_000_000.5,
            {
                "session.id": "sess-1",
                "agent": "engineer@1-2",
                "swarm": "rig",
                "lane": "eng",
                "task": "t1",
                "account": "work",
                "hive": "hive-003",
                "harness": "claude",
            },
        )
    ]


def test_an_agent_session_names_its_harness_from_the_hook_target(monkeypatch):
    calls = recorded(monkeypatch)
    presence.agent("sess-2", {**ENV, "AGENTIHOOKS_TARGET": " Codex "}, 5.0)
    assert calls[0][2]["harness"] == "codex"


def test_an_event_without_a_session_marks_nothing(monkeypatch):
    calls = recorded(monkeypatch)
    presence.agent("", ENV, 5.0)
    assert calls == []


def test_a_hook_payload_marks_its_session_under_the_process_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(presence, "time", SimpleNamespace(time=lambda: 9.5))
    monkeypatch.setattr(presence, "agent", lambda *args: calls.append(args))
    presence.mark({"session_id": "sess-3"})
    presence.mark({"session_id": None})
    assert calls == [("sess-3", presence.os.environ, 9.5), ("", presence.os.environ, 9.5)]


def test_a_failing_mark_never_raises_into_the_hook(monkeypatch):
    def fail(*args):
        raise OSError("seat unreadable")

    monkeypatch.setattr(presence, "agent", fail)
    assert presence.mark({"session_id": "sess-4"}) is None


def test_the_hook_manager_marks_presence_for_every_event():
    import inspect

    from hooks import hook_manager

    assert "presence.mark(payload)" in inspect.getsource(hook_manager.main)


def test_the_hive_id_falls_back_to_the_seeded_id_then_the_host_name(monkeypatch):
    from scripts.hive import registry

    seeded = iter(["seeded-1", ""])
    monkeypatch.setattr(registry, "seeded_id", lambda: next(seeded))
    monkeypatch.setattr(presence, "socket", SimpleNamespace(gethostname=lambda: "pod-a"))
    assert presence.hive_id({"AGENTIHOOKS_HIVE_ID": "h1", "SWARM_HIVE_ID": "h2"}) == "h1"
    assert presence.hive_id({"SWARM_HIVE_ID": "h2"}) == "h2"
    assert presence.hive_id({}) == "seeded-1"
    assert presence.hive_id({}) == "pod-a"


def test_a_hive_is_marked_seen_with_its_agents_per_harness(monkeypatch):
    calls = recorded(monkeypatch)
    presence.hive("hive-1", "automation", True, "1.2.3", {"claude": 3, "codex": 0}, 42.0)
    attrs = {"hive.id": "hive-1", "hive.name": "automation", "hive.headed": "yes", "hive.release": "1.2.3"}
    assert calls == [
        ("agentihooks.presence.hive", 42.0, attrs),
        ("agentihooks.presence.hive.agents", 3, {**attrs, "harness": "claude"}),
        ("agentihooks.presence.hive.agents", 0, {**attrs, "harness": "codex"}),
    ]


def test_a_headless_hive_is_marked_not_headed(monkeypatch):
    calls = recorded(monkeypatch)
    presence.hive("hive-2", "anton", False, "1.2.3", {}, 42.0)
    assert calls == [
        (
            "agentihooks.presence.hive",
            42.0,
            {"hive.id": "hive-2", "hive.name": "anton", "hive.headed": "no", "hive.release": "1.2.3"},
        )
    ]
