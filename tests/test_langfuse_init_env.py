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
        stream.write("LANGFUSE_MCP_URL=http://operator.example/mcp/\\n")
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
