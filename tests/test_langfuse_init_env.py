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
        settings = {"_agentihooks": settings}
    calls = []
    assert seed(lambda: calls.append(True) or ["OTHER"], destination, settings) == ["OTHER"]
    assert calls == [True]
    assert dotenv_values(destination) == {"OTHER": "value", "LANGFUSE_MCP_URL": endpoint}
    before = destination.read_text()
    seed(lambda: [], destination, settings)
    assert destination.read_text() == before
    env = settings.get("env", settings.get("_agentihooks", {}).get("env"))
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
