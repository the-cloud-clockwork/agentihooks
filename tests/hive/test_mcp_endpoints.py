import json

import pytest

from tests.test_profile_render import world as world

pytestmark = pytest.mark.unit


def remote_profile(world):
    path = world["role"] / ".claude/.mcp.json"
    servers = {
        "memory": {"type": "http", "url": "http://localhost:8104/mcp", "headers": {"X-Client": "fixture"}},
        "local": {"command": "local-server"},
    }
    path.write_text(json.dumps({"mcpServers": servers}))
    return servers


def test_hive_mcp_endpoints_override_profile_defaults_without_changing_other_servers(world, monkeypatch):
    from scripts.profiles import render

    original = remote_profile(world)
    endpoint = "http://memory.example.test/mcp"
    monkeypatch.setenv("AGENTIHOOKS_MCP_URLS", json.dumps({"memory": endpoint, "local": endpoint, "absent": endpoint}))
    out = render.render_claude("rb-role")
    servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert servers["memory"] == {**original["memory"], "url": endpoint}
    assert servers["local"] == original["local"]
    assert "absent" not in servers


def test_changed_mcp_endpoints_refresh_cached_profiles_and_removing_them_restores_defaults(world, monkeypatch):
    from scripts.profiles import render

    original = remote_profile(world)
    monkeypatch.delenv("AGENTIHOOKS_MCP_URLS", raising=False)
    first = render.render_claude("rb-role")
    assert json.loads((first / ".claude.json").read_text())["mcpServers"]["memory"] == original["memory"]
    monkeypatch.setenv("AGENTIHOOKS_MCP_URLS", json.dumps({"memory": "http://memory.example.test/mcp"}))
    second = render.render_claude("rb-role")
    assert second is not None
    assert (
        json.loads((second / ".claude.json").read_text())["mcpServers"]["memory"]["url"]
        == "http://memory.example.test/mcp"
    )
    assert render.render_claude("rb-role") is None
    monkeypatch.delenv("AGENTIHOOKS_MCP_URLS")
    third = render.render_claude("rb-role")
    assert third is not None
    assert json.loads((third / ".claude.json").read_text())["mcpServers"]["memory"] == original["memory"]


def test_hive_setup_accepts_explicit_mcp_endpoints_from_the_environment(tmp_path):
    from scripts.hive import setup

    urls = json.dumps({"memory": "http://memory.example.test/mcp"})
    kept, skipped = setup.entries({"AGENTIHOOKS_MCP_URLS": urls}, tmp_path)
    assert kept["AGENTIHOOKS_MCP_URLS"] == urls
    assert skipped == {}


def test_hive_mcp_bearer_environment_references_preserve_profile_settings(world, monkeypatch):
    from scripts.profiles import render

    original = remote_profile(world)
    monkeypatch.setenv(
        "AGENTIHOOKS_MCP_BEARER_ENV",
        json.dumps({"memory": "HIVE_MEMORY_TOKEN", "local": "LOCAL_TOKEN", "absent": "ABSENT_TOKEN"}),
    )
    out = render.render_claude("rb-role")
    servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert servers["memory"] == {
        **original["memory"],
        "headers": {"X-Client": "fixture", "Authorization": "Bearer ${HIVE_MEMORY_TOKEN}"},
    }
    assert servers["local"] == original["local"]
    assert "absent" not in servers


def test_changed_mcp_bearer_references_refresh_and_restore_cached_profiles(world, monkeypatch):
    from scripts.profiles import render

    original = remote_profile(world)
    monkeypatch.setenv("AGENTIHOOKS_MCP_BEARER_ENV", "{}")
    first = render.render_claude("rb-role")
    assert json.loads((first / ".claude.json").read_text())["mcpServers"]["memory"] == original["memory"]
    monkeypatch.setenv("AGENTIHOOKS_MCP_BEARER_ENV", json.dumps({"memory": "HIVE_MEMORY_TOKEN"}))
    second = render.render_claude("rb-role")
    assert second is not None
    assert json.loads((second / ".claude.json").read_text())["mcpServers"]["memory"]["headers"] == {
        "X-Client": "fixture",
        "Authorization": "Bearer ${HIVE_MEMORY_TOKEN}",
    }
    assert render.render_claude("rb-role") is None
    monkeypatch.delenv("AGENTIHOOKS_MCP_BEARER_ENV")
    third = render.render_claude("rb-role")
    assert third is not None
    assert json.loads((third / ".claude.json").read_text())["mcpServers"]["memory"] == original["memory"]


def test_hive_setup_publishes_mcp_bearer_environment_references(tmp_path):
    from scripts.hive import setup

    references = json.dumps({"memory": "HIVE_MEMORY_TOKEN"})
    kept, skipped = setup.entries({"AGENTIHOOKS_MCP_BEARER_ENV": references}, tmp_path)
    assert kept["AGENTIHOOKS_MCP_BEARER_ENV"] == references
    assert skipped == {}


def test_mcp_bearer_reference_works_without_existing_headers(world, monkeypatch):
    from scripts.profiles import render

    original = remote_profile(world)
    del original["memory"]["headers"]
    (world["role"] / ".claude/.mcp.json").write_text(json.dumps({"mcpServers": original}))
    monkeypatch.setenv("AGENTIHOOKS_MCP_BEARER_ENV", json.dumps({"memory": "HIVE_MEMORY_TOKEN"}))
    out = render.render_claude("rb-role")
    servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert servers["memory"] == {**original["memory"], "headers": {"Authorization": "Bearer ${HIVE_MEMORY_TOKEN}"}}
