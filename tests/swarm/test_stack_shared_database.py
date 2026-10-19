from scripts.swarm import stack_env, stack_init


def test_adoption_keeps_shared_connections_in_stack_and_client(tmp_path):
    database = "postgresql://ledger@database.internal/ledger"
    redis = "rediss://swarm.internal:6379/0"
    chosen = {
        "AGENTIHOOKS_SWARM_DATABASE_URL": database,
        "AGENTIHOOKS_SWARM_REDIS_URL": redis,
    }
    (tmp_path / stack_init.PREPARED_CLIENT).write_text("AGENTIHOOKS_SWARM_REDIS_URL=redis://adopted:6379/0\n")
    (tmp_path / "stack").mkdir()
    env_file = tmp_path / "stack" / ".env"
    assert stack_init.adopt(env_file, chosen) is True
    values = stack_env.settings(env_file)
    assert values["SWARM_ADOPT_REDIS_URL"] == "redis://adopted:6379/0"
    assert {key: values[key] for key in chosen} == chosen
    client = dict(line.split("=", 1) for line in stack_env.client_env(values, tmp_path).splitlines())
    assert {key: client[key] for key in chosen} == chosen
    assert client["LEDGER_URL"] == "http://127.0.0.1:8765"


def test_external_clients_keep_database_without_rebuilding_redis_address(tmp_path):
    values = {
        "SWARM_PORT": "8767",
        "SWARM_REDIS_SCHEME": "rediss",
        "AGENTIHOOKS_SWARM_DATABASE_URL": "postgresql://ledger@shared/ledger",
        "AGENTIHOOKS_SWARM_REDIS_URL": "rediss://shared:6379/0",
    }
    assert stack_env.client_env(values, tmp_path) == (
        "AGENTIHOOKS_DEPLOYMENT=compose\n"
        "LEDGER_URL=http://127.0.0.1:8767\n"
        "AGENTIHOOKS_SWARM_REDIS_URL=rediss://shared:6379/0\n"
        "AGENTIHOOKS_SWARM_DATABASE_URL=postgresql://ledger@shared/ledger\n"
    )


def test_initialization_retains_selected_urls_and_shell_quotes_database_options(tmp_path):
    import shlex
    from unittest.mock import patch

    chosen = {
        "AGENTIHOOKS_SWARM_DATABASE_URL": "postgresql://ledger@shared/ledger?sslmode=verify-full&sslrootcert=/certs/ca.crt",
        "AGENTIHOOKS_SWARM_REDIS_URL": "rediss://shared:6379/0",
    }
    env_file = tmp_path / "settings"
    values = stack_env.settings(env_file, chosen)
    assert {key: values[key] for key in chosen} == chosen
    assert {key: stack_env.read_env(env_file)[key] for key in chosen} == chosen
    args = shlex.split(stack_env.client_env(values, tmp_path))
    assert f"AGENTIHOOKS_SWARM_DATABASE_URL={chosen['AGENTIHOOKS_SWARM_DATABASE_URL']}" in args
    before = env_file.read_bytes()
    with patch.object(stack_env, "write_private") as writer:
        assert stack_env.settings(env_file, chosen) == values
        writer.assert_not_called()
    assert env_file.read_bytes() == before


def test_native_initialization_propagates_shared_settings_without_adoption(tmp_path, monkeypatch):
    import io

    from tests.swarm.test_stack_init import Docker

    monkeypatch.setattr(stack_init.shutil, "which", lambda name: f"/usr/bin/{name}")
    chosen = {
        "AGENTIHOOKS_SWARM_DATABASE_URL": "postgresql://ledger@shared/ledger",
        "AGENTIHOOKS_SWARM_REDIS_URL": "rediss://shared:6379/0",
    }
    environ = {"AGENTIHOOKS_HOME": str(tmp_path), **chosen}
    out = io.StringIO()
    assert stack_init.init(["--no-start"], environ, Docker(), None, out, "demo-swarm") == 0
    folder = tmp_path / "swarm-stack"
    assert {key: stack_env.read_env(folder / ".env")[key] for key in chosen} == chosen
    assert {key: stack_env.read_env(folder / "client.env")[key] for key in chosen} == chosen
    assert all(value not in out.getvalue() for value in chosen.values())
