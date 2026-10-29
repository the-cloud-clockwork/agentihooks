import json
import sys
from pathlib import Path

import pytest

from tests.test_profile_render import _write, named_brain, world  # noqa: F401

SWARM_MCP = "https://swarm-brain-mcp.example.test/mcp"


@pytest.fixture
def kernel_brain(world, named_brain, monkeypatch):  # noqa: F811
    from scripts.swarm import brain_binding

    monkeypatch.delenv(brain_binding.VARIABLE, raising=False)
    kernel = {"agentibrain": {"type": "http", "url": "https://kernel-brain.example.test/mcp"}}
    _write(named_brain / ".claude" / ".mcp.json", json.dumps({"mcpServers": kernel}))
    overlay = world["bundle"] / "profiles" / "swarm-brain"
    _write(overlay / "profile.yml", "name: swarm-brain\nkind: overlay\nwears: [engineer]\n")
    _write(overlay / "CLAUDE.md", "SWARM BRAIN MARKER\n")
    swarm = {"agentibrain": {"type": "http", "url": SWARM_MCP}}
    _write(overlay / ".claude" / ".mcp.json", json.dumps({"mcpServers": swarm}))
    profile = world["role"] / "profile.yml"
    profile.write_text(profile.read_text().replace("[brain]", "[brain, swarm-brain]"))
    return kernel["agentibrain"]


def test_a_role_rendered_outside_a_swarm_keeps_the_kernel_brain(world, kernel_brain):  # noqa: F811
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    assert json.loads((out / ".claude.json").read_text())["mcpServers"]["agentibrain"] == kernel_brain
    assert json.loads((out / render.STAMP).read_text())["overlays"] == ["brain"]
    assert "SWARM BRAIN MARKER" not in (out / "CLAUDE.md").read_text()


def test_a_managed_swarm_launch_wears_the_swarm_brain(world, kernel_brain, monkeypatch):  # noqa: F811
    from scripts.profiles import render
    from scripts.swarm import brain_binding

    binding = {"status": "bound", "api": "https://swarm-brain-api.example.test", "api_env": "SWARM_BRAIN_API_TOKEN"}
    binding |= {"mcp": SWARM_MCP, "mcp_header": "X-API-Key", "mcp_scheme": "", "mcp_env": "SWARM_BRAIN_MCP_KEY"}
    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(binding))
    monkeypatch.setattr(world["install"], "_resolve_hooks_python", lambda: Path(sys.executable))

    out = render.render_claude("rb-role")

    assert json.loads((out / ".claude.json").read_text())["mcpServers"]["agentibrain"]["url"] == SWARM_MCP
    assert json.loads((out / render.STAMP).read_text())["overlays"] == ["brain", "swarm-brain"]
    assert "SWARM BRAIN MARKER" in (out / "CLAUDE.md").read_text()


def test_a_managed_launch_with_a_refused_binding_still_wears_the_swarm_brain(world, kernel_brain, monkeypatch):  # noqa: F811
    from scripts.profiles import render
    from scripts.swarm import brain_binding

    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps({"status": "refused", "reason": "unset"}))
    monkeypatch.setattr(world["install"], "_resolve_hooks_python", lambda: Path(sys.executable))

    out = render.render_claude("rb-role")

    assert "agentibrain" not in json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert json.loads((out / render.STAMP).read_text())["overlays"] == ["brain", "swarm-brain"]
