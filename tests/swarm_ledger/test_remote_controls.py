import pytest

from scripts.swarm import commands
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_server as server


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    monkeypatch.setattr(server, "swarm_store", lambda: saved)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **kw: pytest.fail("server ran a workstation tool"))
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: pytest.fail("server launched a workstation tool"))
    return saved


def test_server_controls_only_queue_work_on_a_tool_free_path(store):
    status, error = server.swarm_control("sw", ["pause"])
    assert error == ""
    assert status["commands"][0]["state"] == "pending"
    assert store.config("sw").state == "paused"
    assert server.refresh_quota("sw")[1] == ""
    assert commands.rows(store, "sw")[-1]["command"] == "quota"
    assert server.swarm_control("sw", ["stop"], "doctor")[1] == ""
    assert commands.rows(store, "sw")[-1]["command"] == "doctor"


def test_quota_refresh_wakes_the_hive_prober_the_moment_it_is_queued(store):
    from scripts.swarm import quota_refresh

    pubsub = store.redis.pubsub()
    pubsub.subscribe(quota_refresh.WAKE)
    assert pubsub.get_message(timeout=1)["type"] == "subscribe"
    status, error = server.refresh_quota("sw")
    assert error == ""
    assert [(row["command"], row["state"]) for row in status["commands"]] == [("quota", "pending")]
    assert pubsub.get_message(timeout=1)["data"] == "sw"


def test_a_refresh_click_shows_the_new_probe_in_the_next_status_after_one_prober_round(store, monkeypatch):
    from scripts import agents_quota
    from scripts.swarm import quota_refresh

    probed = {"probed_at": 50, "rows": ["probed"]}
    monkeypatch.setattr(agents_quota, "page_quota", lambda: probed)
    commands.publish(store, "sw", {"agents": [], "tasks": {}, "quota": {"probed_at": 10, "rows": ["tick"]}}, {})
    assert server.refresh_quota("sw")[0]["quota"]["rows"] == ["tick"]
    assert quota_refresh.answer_all(store, lambda slug, row: "") == ["sw: control quota acknowledged"]
    assert server.swarm_status("sw")["quota"] == probed


def test_terminate_checks_membership_then_queues_an_exact_name(store):
    assert server.terminate_control("sw", "outside") == (None, "agent is not in this swarm")
    store.put_agent("sw", AgentRecord("a", "eng", "t", 1))
    assert server.terminate_control("sw", "a")[1] == ""
    assert commands.rows(store, "sw")[0]["argv"] == ["terminate", "a"]
    assert commands.rows(store, "sw")[0]["command"] == "swarm"


def test_server_reads_published_workspaces_and_quota_without_home_files(store, monkeypatch):
    commands.publish(store, "sw", {"quota": {"rows": ["remote"]}}, {"t": {"latest_progress": "remote step"}})
    monkeypatch.setattr("scripts.swarm_ledger.ledger_workspace.tails", lambda *a: pytest.fail("server read hive files"))
    assert server.swarm_status("sw")["quota"]["rows"] == ["remote"]
    assert server.workspace_tails("sw", "t") == {"latest_progress": "remote step"}


def test_operator_doctor_stop_phrase_queues_to_the_owner(store):
    server.doctor_phrase(
        "sw",
        {"_meta": {"rev": 1, "events": [{"rev": 1, "by": "operator", "target": "chat", "text": server.DOCTOR_PHRASE}]}},
    )
    assert commands.rows(store, "sw")[0]["argv"] == ["stop"]
    assert commands.rows(store, "sw")[0]["command"] == "doctor"


def test_chat_events_without_text_do_not_stop_the_doctor(store):
    server.doctor_phrase("sw", {"_meta": {"rev": 1, "events": [{"rev": 1, "by": "operator", "target": "chat"}]}})
    assert commands.rows(store, "sw") == []


def test_missing_work_tail_is_an_empty_map(store):
    assert server.workspace_tails("sw", "t") == {}


def test_published_work_tail_ids_can_have_uppercase_letters(store):
    commands.publish(store, "sw", {}, {"UPPER": {"latest_progress": "step"}})
    assert server.workspace_tails("sw", "UPPER") == {"latest_progress": "step"}


def test_unsafe_workspace_id_is_refused_without_a_hive_read(store, monkeypatch):
    monkeypatch.setattr(commands, "workspaces", lambda *a: pytest.fail("read the hive"))
    with pytest.raises(ValueError):
        server.workspace_tails("sw", "../bad")
