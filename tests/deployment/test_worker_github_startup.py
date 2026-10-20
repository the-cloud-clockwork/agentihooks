import os
from pathlib import Path

from tests.deployment.test_worker_hive import startup


def test_live_worker_start_mints_before_bundle_clone_without_saving_the_app_credentials(tmp_path, monkeypatch):
    fake = tmp_path / "network"
    fake.mkdir()
    marker = tmp_path / "minted"
    (fake / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\nfrom types import SimpleNamespace\nimport jwt, requests\n"
        "jwt.encode = lambda *a, **k: 'fixture'\n"
        "def post(*a, **k):\n"
        "    Path(os.environ['APP_PROOF_PATH']).touch()\n"
        "    return SimpleNamespace(status_code=201, json=lambda: {'token': 'fixture', 'expires_at': '2099-01-01T00:00:00Z'})\n"
        "requests.post = post\n"
    )
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(fake), str(root), os.environ.get("PYTHONPATH", ""))))
    monkeypatch.setenv("APP_PROOF_PATH", str(marker))
    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", "app")
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    result, _, home, repo = startup(tmp_path, bundle=True)
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert (repo.parent / "agentihooks-bundle/.git").is_dir()
    for path in [home / "hive-container.env", tmp_path / "home/.gitconfig"]:
        text = path.read_text()
        assert "pem" not in text
        assert "fixture" not in text if path.name == ".gitconfig" else "GITHUB_APP_PRIVATE_KEY" not in text
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in (tmp_path / "home/.gitconfig").read_text()
