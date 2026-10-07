import pytest

from tests.test_profile_render import READS, _declare, _gateway
from tests.test_profile_render import advertised as advertised
from tests.test_profile_render import world as world


def test_missing_required_connector_reference_refuses_home_publication(world, advertised, monkeypatch):
    from scripts.profiles import render

    _declare(world, gw=_gateway(enabled_tools=READS))
    monkeypatch.delenv("GW_KEY")
    home = render.rendered_root() / "rb-role" / "claude"

    with pytest.raises(ValueError, match="MCP 'gw'.*environment variable GW_KEY is unset"):
        render.render_claude("rb-role")

    assert not home.exists()
    assert advertised == []


@pytest.mark.parametrize("force", [False, True])
def test_missing_required_reference_preserves_a_complete_home(world, advertised, monkeypatch, force):
    from scripts.profiles import render

    _declare(world, gw=_gateway(enabled_tools=READS))
    home = render.render_claude("rb-role")
    native = (home / ".claude.json").read_bytes()
    stamp = (home / render.STAMP).read_bytes()
    monkeypatch.delenv("GW_KEY")

    with pytest.raises(ValueError, match="environment variable GW_KEY is unset"):
        render.render_claude("rb-role", force=force)

    assert (home / ".claude.json").read_bytes() == native
    assert (home / render.STAMP).read_bytes() == stamp


@pytest.mark.parametrize("role", ["master", "qa"])
@pytest.mark.parametrize("entrypoint", ["render", "init-agent"])
def test_render_and_init_agent_load_connector_environment_before_publication(
    world, advertised, monkeypatch, role, entrypoint
):
    import json
    import os

    from scripts import init_agent
    from scripts.profiles import render
    from tests.test_profile_render import _write

    _declare(world, gw=_gateway(enabled_tools=READS))
    _write(world["bundle"] / "profiles" / role / "profile.yml", f"name: {role}\nextends: [rb-role]\n")
    monkeypatch.delenv("GW_KEY")
    monkeypatch.delenv("GW_SCOPE")
    world["install"]._ENV_FILE_DST.write_text("GW_KEY=k-test\n")
    (world["install"]._ENV_FILE_DST.parent / "connector.env").write_text("GW_SCOPE=s-test\n")

    if entrypoint == "render":
        assert render.main(["render", role]) == 0
    else:
        assert (
            init_agent.main(
                ["--profile", role, "--agent", "claude", "--host", "herdr", "--dir", str(world["home"]), "--dry-run"]
            )
            == 0
        )
    home = render.rendered_root() / role / "claude"
    servers = json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert servers["gw"]["headers"]["Authorization"] == "Bearer ${GW_KEY}"
    assert advertised == [
        ("https://gw.example/mcp/", {"Authorization": "Bearer k-test", "x-mcp-servers": "lf", "X-Scope": "s-test"})
    ]
    assert json.loads((home.parent / "claude.mounts.json").read_text())["gw"] == {
        "mounted": True,
        "enabled_tools": READS,
    }
    assert os.environ["GW_KEY"] == "k-test"
    assert os.environ["GW_SCOPE"] == "s-test"


@pytest.mark.parametrize("corruption", ["manifest", "server", "both", "missing-manifest-entry", "missing-server-map"])
def test_incomplete_cached_connector_is_repaired(world, advertised, corruption):
    import json

    from scripts.profiles import render

    _declare(world, gw=_gateway(enabled_tools=READS))
    home = render.render_claude("rb-role")
    if corruption in ("manifest", "both", "missing-manifest-entry"):
        manifest = home.parent / "claude.mounts.json"
        mounts = json.loads(manifest.read_text())
        if corruption == "missing-manifest-entry":
            del mounts["gw"]
        else:
            mounts["gw"] = {"mounted": False, "reason": "environment variable GW_KEY is unset"}
        manifest.write_text(json.dumps(mounts))
    if corruption in ("server", "both", "missing-server-map"):
        native = home / ".claude.json"
        doc = json.loads(native.read_text())
        if corruption == "missing-server-map":
            del doc["mcpServers"]
        else:
            del doc["mcpServers"]["gw"]
        native.write_text(json.dumps(doc))

    assert render.render_claude("rb-role") == home
    assert "gw" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert json.loads((home.parent / "claude.mounts.json").read_text())["gw"] == {
        "mounted": True,
        "enabled_tools": READS,
    }
    assert render.render_claude("rb-role") is None


def test_cache_accepts_a_native_map_with_exactly_the_required_connector(world, advertised):
    import json

    from scripts.profiles import render

    _declare(world, gw=_gateway(enabled_tools=READS))
    home = render.render_claude("rb-role")
    native = home / ".claude.json"
    doc = json.loads(native.read_text())
    doc["mcpServers"] = {"gw": doc["mcpServers"]["gw"]}
    native.write_text(json.dumps(doc))

    assert render.render_claude("rb-role") is None
    assert len(advertised) == 1


@pytest.mark.parametrize("previous", [False, True])
def test_tool_listing_failure_leaves_no_cached_home(world, advertised, monkeypatch, previous):
    from scripts.profiles import connectors, render

    _declare(world, gw=_gateway(enabled_tools=READS))
    home = render.rendered_root() / "rb-role" / "claude"
    if previous:
        render.render_claude("rb-role")
    listing = connectors.advertised

    def refuse(url, headers):
        raise ConnectionError("refused")

    monkeypatch.setattr(connectors, "advertised", refuse)
    assert render.render_claude("rb-role", force=True) == home
    assert not (home / render.STAMP).exists()
    monkeypatch.setattr(connectors, "advertised", listing)
    assert render.render_claude("rb-role") == home
    assert render.render_claude("rb-role") is None
