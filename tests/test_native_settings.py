import json
from pathlib import Path

import pytest

from scripts import native_settings as ns
from scripts.targets._common import _install_module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def layers(tmp_path, monkeypatch):
    install = _install_module()
    monkeypatch.setattr(install, "_detect_venv", lambda: tmp_path / "venv" / "bin" / "python")
    bundle = tmp_path / "bundle"
    _write(bundle / ".claude" / "settings.overrides.json", json.dumps({"env": {"LAYER": "bundle", "B": "1"}}))
    role = bundle / "profiles" / "ns-role"
    _write(role / "profile.yml", "name: ns-role\n")
    hook = {"Stop": [{"hooks": [{"type": "command", "command": "bash hooks/stop.sh"}]}]}
    _write(role / ".claude" / "settings.overrides.json", json.dumps({"env": {"LAYER": "role"}, "hooks": hook}))
    extra = bundle / "profiles" / "ns-extra"
    _write(extra / "profile.yml", "name: ns-extra\n")
    _write(extra / ".claude" / "settings.overrides.json", json.dumps({"env": {"LAYER": "extra"}}))
    install._save_state({"bundle": {"path": str(bundle)}})
    monkeypatch.setattr(install, "_load_connectors", lambda name: ({}, [], []))
    return {"install": install, "bundle": bundle, "role": role, "extra": extra}


def test_layers_merge_in_order_and_each_is_announced(layers):
    said = []
    doc = ns.native_settings("claude", [("ns-role", layers["role"])], ("ns-extra", layers["extra"]), say=said.append)
    assert doc["env"]["LAYER"] == "extra"
    assert doc["env"]["B"] == "1"
    assert said == [
        f"Applied bundle overrides: {layers['bundle'] / '.claude' / 'settings.overrides.json'}",
        f"Applied profile overrides: {layers['role'] / '.claude' / 'settings.overrides.json'}",
        "Applied settings-profile overlay: ns-extra (bundle)",
    ]


def test_claude_hook_paths_resolve_against_their_layer(layers):
    doc = ns.native_settings("claude", [("ns-role", layers["role"])], None, say=lambda *_: None)
    commands = [h["command"] for group in doc["hooks"]["Stop"] for h in group["hooks"]]
    assert any(str(layers["role"] / "hooks" / "stop.sh") in command for command in commands)
    assert "bash hooks/stop.sh" not in commands


def test_other_targets_keep_hook_paths_as_written(layers):
    _write(layers["role"] / ".codex" / "config.overrides.toml", '[hooks]\ncommand = "bash hooks/stop.sh"\n')
    doc = ns.native_settings("codex", [("ns-role", layers["role"])], None, say=lambda *_: None)
    assert doc["hooks"]["command"] == "bash hooks/stop.sh"


def test_the_base_layer_carries_the_detected_python(layers, tmp_path):
    doc = ns.native_settings("claude", [("ns-role", layers["role"])], None, say=lambda *_: None)
    assert str(tmp_path / "venv" / "bin" / "python") in json.dumps(doc)
    assert "__PYTHON__" not in json.dumps(doc)


def test_without_a_say_the_installer_prints(layers, capsys):
    ns.native_settings("claude", [("ns-role", layers["role"])], None)
    assert "Applied profile overrides: " in capsys.readouterr().out


def test_connectors_of_the_last_profile_merge_in(layers, monkeypatch):
    asked = []

    def connectors(name):
        asked.append(name)
        return {"CONN": "1"}, ["Bash(rm:*)"], ["srv-b", "srv-a"]

    monkeypatch.setattr(layers["install"], "_load_connectors", connectors)
    said = []
    dirs = [("ns-extra", layers["extra"]), ("ns-role", layers["role"])]
    doc = ns.native_settings("claude", dirs, None, say=said.append)
    assert asked == ["ns-role"]
    assert doc["env"]["CONN"] == "1"
    assert doc["permissions"]["deny"][-1] == "Bash(rm:*)"
    assert doc["disabledMcpjsonServers"][-2:] == ["srv-b", "srv-a"]
    assert said[-3:] == [
        "  [OK] Connector env: ['CONN']",
        "  [OK] Connector deny rules: 1",
        f"  [OK] Connector disabled MCP servers: {doc['disabledMcpjsonServers']}",
    ]


def test_disabled_servers_keep_existing_entries_once(layers, monkeypatch):
    _write(
        layers["role"] / ".claude" / "settings.overrides.json",
        json.dumps({"disabledMcpjsonServers": ["srv-a", "srv-c"]}),
    )
    monkeypatch.setattr(layers["install"], "_load_connectors", lambda name: ({}, [], ["srv-a", "srv-b"]))
    doc = ns.native_settings("claude", [("ns-role", layers["role"])], None, say=lambda *_: None)
    assert doc["disabledMcpjsonServers"][-3:] == ["srv-a", "srv-c", "srv-b"]
    assert doc["disabledMcpjsonServers"].count("srv-a") == 1


def test_no_connectors_announce_nothing(layers):
    said = []
    ns.native_settings("claude", [("ns-role", layers["role"])], None, say=said.append)
    assert not any("Connector" in line for line in said)
