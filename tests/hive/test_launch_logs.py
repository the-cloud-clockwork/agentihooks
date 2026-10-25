import io
import sys
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


def test_hive_codex_server_launch_captures_its_receipt(tmp_path):
    from scripts import init_agent

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

    def lookup(slug, execution):
        assert (slug, execution) == ("fixture", "execution-one")
        return agent

    monkeypatch.setattr(store.execution_registry, "get", lookup)
    monkeypatch.setattr(launch_logs, "connect", lambda: store)
    sensitive = str(uuid.uuid4())
    local_log = tmp_path / "server.log"
    local_log.write_text(f"upstream connection refused\npassword={sensitive}\n")
    process = SimpleNamespace(stdout=io.StringIO(local_log.read_text()), wait=lambda: 3)

    def spawn(argv, **kwargs):
        from scripts.hive.orders import Orders

        receipt = Orders(store.redis).launch_receipt("hive-one", "fixture", "execution-one", 7)
        assert store.redis.hgetall(receipt) == {"supervisor_log": ""}
        assert argv == ["server"]
        assert kwargs == {
            "stdout": launch_logs.subprocess.PIPE,
            "stderr": launch_logs.subprocess.STDOUT,
            "text": True,
            "errors": "replace",
        }
        return nullcontext(process)

    monkeypatch.setattr(launch_logs.subprocess, "Popen", spawn)

    class Output(io.StringIO):
        flushed = False

        def flush(self):
            self.flushed = True

    output = Output()
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
    assert output.flushed
    assert replacement.redis.ttl(receipt) == -1
    assert not replacement.redis.exists(
        Orders(replacement.redis).launch_receipt("hive-one", "fixture", "execution-one", 8)
    )


@pytest.mark.parametrize("kind", ["", "RSA ", "OPENSSH "])
@pytest.mark.parametrize("closed", [True, False])
def test_multiline_keys_are_redacted_before_any_output(kind, closed):
    from scripts.hive.launch_logs import redacted_lines

    body = str(uuid.uuid4())
    begin = "-" * 5 + f"BEGIN {kind}PRIVATE KEY" + "-" * 5
    end = "-" * 5 + f"END {kind}PRIVATE KEY" + "-" * 5
    lines = ["launch starting\n", begin + "\n", body + "\n"]
    if closed:
        lines += [end + "\n", "connection refused\n"]
    clean = list(redacted_lines(lines))
    assert (
        clean == ["launch starting\n", "[REDACTED:private_key]\n", "connection refused\n"]
        if closed
        else clean == ["launch starting\n", "[REDACTED:private_key]"]
    )
    assert body not in "".join(clean)


def test_log_is_persisted_before_server_exit_and_retention_is_bounded(monkeypatch):
    import fakeredis

    from scripts.hive import launch_logs
    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = SimpleNamespace(execution_id="execution", generation=2, runtime_target={"host_id": "hive"})
    monkeypatch.setattr(store.execution_registry, "get", lambda slug, execution: agent)
    monkeypatch.setattr(launch_logs, "connect", lambda: store)
    monkeypatch.setattr(
        launch_logs.sys, "argv", ["capture", "--slug", "fixture", "--execution", "execution", "--", "server"]
    )
    from scripts.hive.orders import Orders

    receipt = Orders(store.redis).launch_receipt("hive", "fixture", "execution", 2)
    first = "a" * launch_logs.LOG_LIMIT + "\n"

    def lines():
        yield first
        assert store.redis.hget(receipt, "supervisor_log") == "a" * (launch_logs.LOG_LIMIT - 1) + "\n"
        yield "failed\n"

    process = SimpleNamespace(stdout=lines(), wait=lambda: 9)
    monkeypatch.setattr(launch_logs.subprocess, "Popen", lambda *args, **kwargs: nullcontext(process))
    monkeypatch.setattr(launch_logs.sys, "stdout", io.StringIO())
    assert launch_logs.main() == 9
    assert store.redis.hget(receipt, "supervisor_log") == "a" * (launch_logs.LOG_LIMIT - 8) + "\nfailed\n"


@pytest.mark.parametrize("missing", ["--slug", "--execution"])
def test_capture_requires_both_receipt_identity_fields(missing):
    from scripts.hive.launch_logs import main

    present = "--execution" if missing == "--slug" else "--slug"
    with pytest.raises(SystemExit) as failure:
        main([present, "fixture", "--", "server"])
    assert failure.value.code == 2


@pytest.mark.parametrize("closed", [True, False])
def test_log_redaction_is_strict_even_when_the_configured_mode_is_off(closed, monkeypatch):
    from hooks import config
    from scripts.hive.launch_logs import redacted_lines

    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    sensitive = uuid.uuid4().hex
    begin = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
    end = "-" * 5 + "END PRIVATE KEY" + "-" * 5
    lines = [f"Bearer {sensitive} {begin}\n", "private body\n"]
    if closed:
        lines.append(end + "\n")
    assert list(redacted_lines(lines)) == ["[REDACTED:bearer_value] [REDACTED:private_key]" + ("\n" if closed else "")]
