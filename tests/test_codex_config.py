"""The codex config builder keeps layer settings and leaves server tables to the connector pass."""

from pathlib import Path

from scripts.profiles import render


def test_codex_config_drops_the_layer_mcp_servers_table(tmp_path: Path) -> None:
    settings = {"features": {}, "mcp_servers": {"layer-srv": {"command": "x"}}, "model": "m"}

    doc = render._codex_config({}, tmp_path / "operator", tmp_path / "out", settings)

    assert "mcp_servers" not in doc
    assert doc["model"] == "m"
