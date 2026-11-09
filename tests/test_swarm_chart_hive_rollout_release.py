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


@pytest.mark.feature("hive-roll-hooks-run-the-chart-release-worker")
def test_the_rollout_hooks_run_the_hive_worker_tag_the_chart_release_stamps() -> None:
    template = (CHART / "templates" / "hive-rollout.yaml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "chart-release.yml").read_text()
    publish = (ROOT / "deploy" / "worker" / "release-hive.sh").read_text()

    assert '--app-version "$version"' in workflow
    assert '"$image:$version"' in publish
    assert 'merge (dict "tag" (toString .Chart.AppVersion)) $workers.image' in template
    assert '(dict "image" $hookImage)' in template
    assert '(dict "image" $workers.image)' not in template
    assert "sha256" not in template
