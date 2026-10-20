import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
CHART = Path(__file__).resolve().parents[2] / "deploy/helm/agentihooks-swarm"


def render(tmp_path, mode):
    values = tmp_path / "values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "workers": {
                    "replicas": 1,
                    "credentialSecret": {"name": "member", "key": "credential"},
                    "githubAuth": {
                        "mode": mode,
                        "secretName": "github-access",
                        "tokenKey": "pat",
                        "appIdKey": "id",
                        "installationIdKey": "inst",
                        "privateKeyKey": "pem",
                    },
                }
            }
        )
    )
    return subprocess.run(
        ["helm", "template", "fixture", str(CHART), "-f", str(values)], capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "mode,fields",
    [
        ("token", {"GITHUB_TOKEN": "pat"}),
        ("app", {"GITHUB_APP_ID": "id", "GITHUB_APP_INSTALLATION_ID": "inst", "GITHUB_APP_PRIVATE_KEY": "pem"}),
    ],
)
def test_chart_selects_only_the_requested_github_secret_keys(tmp_path, mode, fields):
    result = render(tmp_path, mode)
    assert result.returncode == 0, result.stderr
    objects = list(yaml.safe_load_all(result.stdout))
    worker = next(
        obj
        for obj in objects
        if obj and obj.get("kind") == "Deployment" and obj["metadata"]["name"].endswith("worker-hive")
    )
    env = {row["name"]: row for row in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HIVE_GITHUB_AUTH_MODE"]["value"] == mode
    selected = {key: row["valueFrom"]["secretKeyRef"] for key, row in env.items() if key.startswith("GITHUB_")}
    assert selected == {key: {"name": "github-access", "key": value} for key, value in fields.items()}


def test_chart_refuses_an_unknown_github_mode(tmp_path):
    result = render(tmp_path, "other")
    assert result.returncode != 0
    assert "workers.githubAuth.mode must be token or app" in result.stderr
