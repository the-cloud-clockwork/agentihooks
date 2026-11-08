from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "agentihooks-swarm"


@pytest.mark.feature("hive-pods-not-rolled-by-release-image-digest")
def test_the_release_annotation_only_follows_an_explicit_rollout_release() -> None:
    template = (CHART / "templates" / "worker-hive.yaml").read_text()
    values = (CHART / "values.yaml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "chart-release.yml").read_text()

    assert "imageDigest" not in template
    assert "imageDigest" not in values
    assert "workers.rollout.release" in template
    assert "stamp-worker-digest.sh" not in workflow
    assert not (CHART / "ci" / "stamp-worker-digest.sh").exists()
