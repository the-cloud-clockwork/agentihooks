import io
import sys
import uuid
from types import SimpleNamespace

from scripts import init_agent


def test_hive_codex_server_launch_captures_its_receipt(tmp_path):
    spec = init_agent.AgentSpec(agent="codex", profile="engineer", execution="execution-one")
    command = init_agent._app_server_command(
        spec,
        tmp_path / "route",
        [],
        {"AGENTIHOOKS_SWARM": "fixture", "AGENTIHOOKS_SLOT_HIVE": "hive-one"},
        tmp_path,
        None,
    )
    assert command[:10] == [
        sys.executable,
        "-m",
        "scripts.hive.launch_logs",
        "--slug",
        "fixture",
        "--execution",
        "execution-one",
        "--",
        sys.executable,
        "-m",
    ]
    assert command[10:12] == ["scripts.codex_session", "serve"]


def test_failed_server_log_survives_pod_files_and_keeps_receipt_generation(tmp_path, monkeypatch):
    import fakeredis

    from scripts.hive import launch_logs
    from scripts.hive.orders import Orders
    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = SimpleNamespace(execution_id="execution-one", generation=7, runtime_target={"host_id": "hive-one"})
    monkeypatch.setattr(store.execution_registry, "get", lambda slug, execution: agent)
    monkeypatch.setattr(launch_logs, "connect", lambda: store)
    sensitive = str(uuid.uuid4())
    local_log = tmp_path / "server.log"
    local_log.write_text(f"upstream connection refused\npassword={sensitive}\n")
    process = SimpleNamespace(stdout=io.StringIO(local_log.read_text()), wait=lambda: 3)
    monkeypatch.setattr(launch_logs.subprocess, "Popen", lambda *args, **kwargs: process)
    output = io.StringIO()
    monkeypatch.setattr(launch_logs.sys, "stdout", output)

    assert launch_logs.main(["--slug", "fixture", "--execution", "execution-one", "--", "server"]) == 3
    local_log.unlink()
    replacement = RedisStore(store.redis)
    receipt = Orders(replacement.redis).launch_receipt("hive-one", "fixture", "execution-one", 7)
    retained = replacement.redis.hget(receipt, "supervisor_log")
    assert "upstream connection refused\n" in retained
    assert sensitive not in retained
    assert "REDACTED" in retained
    assert output.getvalue() == retained
    assert replacement.redis.ttl(receipt) == -1
    assert not replacement.redis.exists(
        Orders(replacement.redis).launch_receipt("hive-one", "fixture", "execution-one", 8)
    )
