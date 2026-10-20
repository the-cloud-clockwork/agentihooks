import json
import os
import subprocess
import sys
from pathlib import Path


def test_generated_hive_launcher_passes_fresh_credentials_to_its_native_child(tmp_path, monkeypatch):
    from scripts import init_agent

    network = tmp_path / "network"
    network.mkdir()
    (network / "sitecustomize.py").write_text(
        "from types import SimpleNamespace\nimport jwt, requests\n"
        "jwt.encode = lambda *a, **k: 'fixture'\n"
        "requests.post = lambda *a, **k: SimpleNamespace(status_code=201, json=lambda: {'token': 'fixture', 'expires_at': '2099-01-01T00:00:00Z'})\n"
    )
    receipt = tmp_path / "receipt.json"
    child = tmp_path / "native.py"
    child.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "Path(os.environ['RECEIPT_PATH']).write_text(json.dumps({"
        "'gh': os.environ.get('GH_TOKEN') == 'fixture',"
        "'github': os.environ.get('GITHUB_TOKEN') == 'fixture',"
        "'helper': any(value == '!python3 -m scripts.github_app_cli credential' for key, value in os.environ.items() if key.startswith('GIT_CONFIG_VALUE_'))}))\n"
    )
    monkeypatch.setattr(init_agent, "_agent_command", lambda *args: ([sys.executable, str(child)], ""))
    root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(tmp_path),
        "SHELL": "/bin/true",
        "HIVE_GITHUB_AUTH_MODE": "app",
        "GITHUB_APP_ID": "123",
        "GITHUB_APP_INSTALLATION_ID": "456",
        "GITHUB_APP_PRIVATE_KEY": "pem",
        "GH_TOKEN": "stale",
        "GITHUB_TOKEN": "stale",
        "RECEIPT_PATH": str(receipt),
        "PYTHONPATH": os.pathsep.join((str(network), str(root), os.environ.get("PYTHONPATH", ""))),
    }
    launcher, _ = init_agent._write_launcher(tmp_path, "fixture", "", [], env)
    result = subprocess.run(["bash", str(launcher)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(receipt.read_text()) == {"gh": True, "github": True, "helper": True}
    assert "fixture" not in result.stdout + result.stderr
