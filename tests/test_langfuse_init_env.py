import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("native", ["claude", "codex", "copilot"])
def test_init_exports_the_configured_endpoint_and_refreshes_its_owned_value(tmp_path, native):
    from dotenv import dotenv_values

    from scripts.runtime_env import seed

    destination = tmp_path / ".env"
    destination.write_text("# Defaults\nOTHER=value\n")
    endpoint = "http://configured.example/mcp/"
    settings = {"env": {"LANGFUSE_MCP_URL": endpoint}}
    if native != "claude":
        settings = {"_agentihooks": settings, "env": {"OTHER": "connector"}}
    calls = []
    assert seed(lambda: calls.append(True) or ["OTHER"], destination, settings) == ["OTHER"]
    assert calls == [True]
    assert dotenv_values(destination) == {"OTHER": "value", "LANGFUSE_MCP_URL": endpoint}
    before = destination.read_text()
    seed(lambda: [], destination, settings)
    assert destination.read_text() == before
    env = settings["env"] if native == "claude" else settings["_agentihooks"]["env"]
    env["LANGFUSE_MCP_URL"] = "http://changed.example/mcp/"
    seed(lambda: [], destination, settings)
    assert dotenv_values(destination)["LANGFUSE_MCP_URL"] == env["LANGFUSE_MCP_URL"]
    env.clear()
    seed(lambda: [], destination, settings)
    assert dotenv_values(destination) == {"OTHER": "value"}


@pytest.mark.parametrize("override", ["http://operator.example/mcp/", ""])
def test_init_preserves_user_endpoint_overrides(tmp_path, override):
    from dotenv import dotenv_values

    from scripts.runtime_env import seed

    destination = tmp_path / ".env"
    destination.write_text(f"LANGFUSE_MCP_URL={override}\n")
    settings = {"env": {"LANGFUSE_MCP_URL": "http://configured.example/mcp/"}}
    before = destination.read_text()
    seed(lambda: [], destination, settings)
    assert destination.read_text() == before
    assert dotenv_values(destination)["LANGFUSE_MCP_URL"] == override


def test_real_init_seeder_refreshes_owned_endpoint_and_preserves_operator_override(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    from scripts import install, runtime_env

    destination = tmp_path / ".env"
    monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", tmp_path)
    monkeypatch.setattr(install, "_ENV_FILE_DST", destination)
    monkeypatch.setattr(install, "_discover_user_env_defaults", lambda: {"DEFAULT": "value"})
    settings = {"_agentihooks": {"env": {"LANGFUSE_MCP_URL": "http://configured.example/mcp/"}}}
    runtime_env.seed(install._seed_user_env_file, destination, settings)
    assert dotenv_values(destination)["LANGFUSE_MCP_URL"] == "http://configured.example/mcp/"
    settings["_agentihooks"]["env"]["LANGFUSE_MCP_URL"] = "http://changed.example/mcp/"
    runtime_env.seed(install._seed_user_env_file, destination, settings)
    assert dotenv_values(destination)["LANGFUSE_MCP_URL"] == "http://changed.example/mcp/"
    with destination.open("a") as stream:
        stream.write("LANGFUSE_MCP_URL=http://operator.example/mcp/\n")
    runtime_env.seed(install._seed_user_env_file, destination, settings)
    assert dotenv_values(destination)["LANGFUSE_MCP_URL"] == "http://operator.example/mcp/"
    assert runtime_env.START not in destination.read_text()


def test_explicit_blank_endpoint_overrides_native_metadata(tmp_path):
    from dotenv import dotenv_values

    from scripts.runtime_env import seed

    destination = tmp_path / ".env"
    destination.write_text("")
    settings = {"_agentihooks": {"env": {"LANGFUSE_MCP_URL": "http://configured.example/mcp/"}}}
    seed(lambda: [], destination, settings)
    settings["env"] = {"LANGFUSE_MCP_URL": ""}
    seed(lambda: [], destination, settings)
    assert "LANGFUSE_MCP_URL" not in dotenv_values(destination)


@pytest.mark.parametrize("prefix", ["", "  export "])
def test_init_preserves_operator_endpoint_with_query_parameters(tmp_path, prefix):
    from scripts.runtime_env import seed

    destination = tmp_path / ".env"
    body = f"{prefix}LANGFUSE_MCP_URL=http://operator.example/mcp/?scope=summary\n"
    destination.write_text(body)
    seed(lambda: [], destination, {"env": {"LANGFUSE_MCP_URL": "http://configured.example/mcp/"}})
    assert destination.read_text() == body


def test_init_creates_a_private_environment_file_without_an_existing_seed(tmp_path):
    import stat

    from dotenv import dotenv_values

    from scripts.runtime_env import seed

    destination = tmp_path / ".env"
    endpoint = "http://configured.example/mcp/"
    seed(lambda: [], destination, {"env": {"LANGFUSE_MCP_URL": endpoint}})
    assert dotenv_values(destination) == {"LANGFUSE_MCP_URL": endpoint}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("after", ["X_NOTE=kept\n", "  X_NOTE=kept\n"])
def test_init_preserves_operator_lines_surrounding_its_owned_block(tmp_path, after):
    from dotenv import dotenv_values

    from scripts import runtime_env

    destination = tmp_path / ".env"
    destination.write_text("  BEFORE=kept\n")
    settings = {"env": {"LANGFUSE_MCP_URL": "http://configured.example/mcp/"}}
    runtime_env.seed(lambda: [], destination, settings)
    with destination.open("a") as stream:
        stream.write(f"\n\n{after}")
    runtime_env.seed(lambda: [], destination, settings)
    assert destination.read_text().startswith(f"  BEFORE=kept\n{after.rstrip()}\n\n")
    assert dotenv_values(destination) == {
        "BEFORE": "kept",
        "X_NOTE": "kept",
        "LANGFUSE_MCP_URL": settings["env"]["LANGFUSE_MCP_URL"],
    }


def test_init_preserves_comments_describing_its_markers_after_the_owned_block(tmp_path):
    from dotenv import dotenv_values

    from scripts import runtime_env

    destination = tmp_path / ".env"
    destination.write_text("BEFORE=kept\n")
    settings = {"env": {"LANGFUSE_MCP_URL": "http://configured.example/mcp/"}}
    runtime_env.seed(lambda: [], destination, settings)
    notes = f"# Note: {runtime_env.START} describes the owned block\nX_NOTE=kept\n# Note: {runtime_env.END} ends it\n"
    with destination.open("a") as stream:
        stream.write(notes)
    settings["env"]["LANGFUSE_MCP_URL"] = "http://changed.example/mcp/"
    runtime_env.seed(lambda: [], destination, settings)
    runtime_env.seed(lambda: [], destination, settings)
    assert notes in destination.read_text()
    assert dotenv_values(destination) == {
        "BEFORE": "kept",
        "X_NOTE": "kept",
        "LANGFUSE_MCP_URL": "http://changed.example/mcp/",
    }
