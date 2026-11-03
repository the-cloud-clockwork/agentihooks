import json
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scripts.swarm import brain_binding
from tests.deployment.test_worker_hive import startup
from tests.test_swarm_brain_binding import _document

pytestmark = pytest.mark.unit


def test_projected_binding_is_installed_as_an_owned_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    projected = tmp_path / "projected.json"
    projected.write_text(json.dumps(_document()))
    projected.chmod(0o440)
    source = tmp_path / "binding.json"
    source.symlink_to(projected)
    monkeypatch.setenv("HIVE_BRAIN_BINDING_FILE", str(source))
    monkeypatch.setenv("SWARM_BRAIN_API_TOKEN", "fixture-brain-api")
    monkeypatch.setenv("SWARM_BRAIN_MCP_KEY", "fixture-brain-mcp")
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    installed = home / "swarm/brain-binding.json"
    assert installed.read_bytes() == projected.read_bytes()
    assert not installed.is_symlink()
    assert installed.stat().st_uid == os.getuid()
    assert installed.stat().st_mode & 0o777 == 0o600
    saved = dotenv_values(home / "hive-container.env")
    bound = brain_binding.resolve({"AGENTIHOOKS_HOME": str(home)}, credentials=saved)
    assert bound["status"] == "bound"
    assert bound["api_env"] == "SWARM_BRAIN_API_TOKEN"
    assert bound["mcp_env"] == "SWARM_BRAIN_MCP_KEY"
    assert "fixture-brain-api" not in installed.read_text()
    assert "fixture-brain-mcp" not in installed.read_text()


def test_missing_projected_binding_refuses_worker_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVE_BRAIN_BINDING_FILE", str(tmp_path / "missing.json"))
    result, commands, _, _ = startup(tmp_path)
    assert result.returncode != 0
    assert "hive run" not in commands
    assert "init" not in commands


def test_unconfigured_worker_keeps_missing_binding_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVE_BRAIN_BINDING_FILE", raising=False)
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    with pytest.raises(brain_binding.Refused, match="no swarm brain binding"):
        brain_binding.resolve({"AGENTIHOOKS_HOME": str(home)})
