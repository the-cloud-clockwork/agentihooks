import json

import pytest

from tests.test_profile_render import _write, overlays, world  # noqa: F401

HEADED = "name: ov-a\nkind: overlay\nwears: [engineer]\nrequired_capabilities: [headed]\n"


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_capability_overlay_refuses_before_rendering(world, overlays, monkeypatch, target):  # noqa: F811
    from scripts.profiles import render

    _write(overlays / "ov-a" / "profile.yml", HEADED)
    monkeypatch.delenv("AGENTIHOOKS_HIVE_CAPABILITIES", raising=False)
    with pytest.raises(ValueError, match="overlay ov-a requires hive capabilities: headed"):
        render.render(target, "rb-eng", overlays=["ov-a"])
    assert render.profile_dir("rb-eng", ["ov-a"]) is None


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_capability_overlay_rechecks_cached_home(world, overlays, monkeypatch, target):  # noqa: F811
    from scripts.profiles import render

    _write(overlays / "ov-a" / "profile.yml", HEADED)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_CAPABILITIES", "linux, headed")
    assert render.render(target, "rb-eng", overlays=["ov-a"]) is not None
    assert render.render(target, "rb-eng", overlays=["ov-a"]) is None
    monkeypatch.setenv("AGENTIHOOKS_HIVE_CAPABILITIES", "headless")
    with pytest.raises(ValueError, match="requires hive capabilities: headed"):
        render.render(target, "rb-eng", overlays=["ov-a"])


@pytest.mark.parametrize("required", ["headed", "[headed, 1]", "false", "{headed: true}"])
def test_capability_overlay_refuses_invalid_requirement(world, overlays, monkeypatch, required):  # noqa: F811
    from scripts.profiles import render

    _write(
        overlays / "ov-a" / "profile.yml",
        f"name: ov-a\nkind: overlay\nwears: [engineer]\nrequired_capabilities: {required}\n",
    )
    monkeypatch.setenv("AGENTIHOOKS_HIVE_CAPABILITIES", "headed")
    with pytest.raises(ValueError, match="required_capabilities must be a list of nonempty strings"):
        render.render_claude("rb-eng", overlays=["ov-a"])


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_headed_overlay_keeps_the_vm_browser(world, overlays, monkeypatch, target):  # noqa: F811
    from scripts.profiles import render

    _write(
        overlays / "headed" / "profile.yml",
        "name: headed\nkind: overlay\nwears: [engineer]\nrequired_capabilities: [headed]\n",
    )
    headed = {"command": "playwright-mcp", "args": ["--cdp-endpoint", "http://127.0.0.1:9222"]}
    extension = {"command": "cmd.exe", "args": ["/c", "playwright-mcp", "--extension"]}
    _write(overlays / "headed" / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"playwright-headed": headed}}))
    _write(
        overlays / "rb-eng" / ".claude" / ".mcp.json",
        json.dumps({"mcpServers": {"playwright-ext-operator": extension}}),
    )
    monkeypatch.setenv("AGENTIHOOKS_HIVE_CAPABILITIES", "headed")
    dirs = render._chain("rb-eng", ["headed"])
    servers = render._mcp_servers(target, world["bundle"], dirs)
    assert servers["playwright-headed"] == headed
    assert "playwright-ext-operator" not in servers
    normal = render._mcp_servers(target, world["bundle"], render._chain("rb-eng"))
    assert "playwright-headed" not in normal
    assert normal["playwright-ext-operator"] == extension
