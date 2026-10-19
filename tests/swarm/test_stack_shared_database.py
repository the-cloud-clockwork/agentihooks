from scripts.swarm import stack_env, stack_init


def test_adoption_keeps_shared_connections_in_stack_and_client(tmp_path):
    database = "postgresql://ledger@database.internal/ledger"
    redis = "rediss://swarm.internal:6379/0"
    chosen = {
        "AGENTIHOOKS_SWARM_DATABASE_URL": database,
        "AGENTIHOOKS_SWARM_REDIS_URL": redis,
    }
    env_file = tmp_path / "settings"
    stack_init.adopt(env_file, chosen)
    values = stack_env.settings(env_file)
    assert {key: values[key] for key in chosen} == chosen
    client = dict(line.split("=", 1) for line in stack_env.client_env(values, tmp_path).splitlines())
    assert {key: client[key] for key in chosen} == chosen
    assert client["LEDGER_URL"] == "http://127.0.0.1:8765"


def test_external_clients_keep_database_without_rebuilding_redis_address(tmp_path):
    values = {
        "SWARM_PORT": "8767",
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
    assert stack_env.settings(env_file, chosen) == values
    assert env_file.read_bytes() == before
