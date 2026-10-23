from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("slug", "commands"),
    [
        (
            "kubernetes-quick-start",
            [
                "oci://ghcr.io/the-cloud-clockwork/charts/agentihooks-swarm",
                "helm upgrade --install",
                "hub:",
                "enabled: true",
                "jetstream:",
                "existingSecret:",
                "kubectl rollout status",
                "helm test",
            ],
        ),
        (
            "single-machine-quick-start",
            [
                "docker compose version",
                "agentihooks swarm init",
                "client.env",
                "NATS",
                "JetStream",
                "agentihooks hive list",
                "agentihooks swarm my-swarm start",
            ],
        ),
        (
            "add-hive-quick-start",
            [
                "agentihooks hive invite",
                "--bus-url",
                "agentihooks hive join",
                "agentihooks hive install",
                "hiveOnly: true",
                "credentialSecret:",
                "SSL_CERT_FILE",
                "agentihooks hive show",
            ],
        ),
    ],
)
def test_install_quickstart_is_published_and_actionable(slug, commands):
    root = Path(__file__).resolve().parents[1]
    docs = root / "docs" / "getting-started"
    page = docs / f"{slug}.md"
    assert page.is_file(), f"Missing quickstart: {slug}"
    _, frontmatter, body = page.read_text().split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["parent"] == "Getting Started"
    assert metadata["permalink"] == f"/docs/getting-started/{slug}/"
    assert f"({slug}.md)" in (docs / "index.md").read_text()
    assert "Prerequisites" in body
    assert "Verify" in body
    assert "Troubleshooting" in body
    for command in commands:
        assert command in body
    config = yaml.safe_load((root / "_config.yml").read_text())
    assert "docs" not in config["exclude"]
    assert page.relative_to(root).as_posix() not in config["exclude"]
