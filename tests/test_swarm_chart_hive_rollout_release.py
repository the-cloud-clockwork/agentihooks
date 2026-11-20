from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "agentihooks-swarm"


@pytest.mark.feature("hive-pods-not-rolled-by-release-image-digest")
def test_the_worker_hive_release_annotation_carries_no_image_digest() -> None:
    template = (CHART / "templates" / "worker-hive.yaml").read_text()
    values = (CHART / "values.yaml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "chart-release.yml").read_text()

    assert "imageDigest" not in template
    assert "imageDigest" not in values
    assert "workers.rollout.release" in template
    assert "stamp-worker-digest.sh" not in workflow
    assert not (CHART / "ci" / "stamp-worker-digest.sh").exists()


@pytest.mark.feature("hive-pods-roll-onto-each-chart-release")
def test_a_leased_release_tag_hive_takes_the_chart_release_version_as_its_stamp() -> None:
    template = (CHART / "templates" / "worker-hive.yaml").read_text()
    smoke = (CHART / "ci" / "kind-smoke.sh").read_text()
    rendered = (ROOT / "tests" / "chart_hive_rollout.py").read_text()
    stamp = (
        '{{- if and (not $release) (eq (toString $workers.image.tag) "release") '
        '(regexMatch "^[0-9]+\\\\.[0-9]+\\\\.[0-9]+$" (toString $root.Chart.AppVersion)) }}'
    )

    assert stamp in template
    assert "$release = toString $root.Chart.AppVersion" in template
    assert "{{- with $release }}" in template
    assert "{{- with $root.Values.workers.rollout.release }}" not in template
    assert "python3 tests/chart_hive_rollout.py" in smoke
    assert "    test_each_released_chart_restamps_a_leased_release_tag_hive_with_its_own_version()\n" in rendered


@pytest.mark.feature("hive-roll-hooks-run-the-chart-release-worker")
def test_the_rollout_hooks_run_the_hive_worker_tag_the_chart_release_stamps() -> None:
    template = (CHART / "templates" / "hive-rollout.yaml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "chart-release.yml").read_text()
    publish = (ROOT / "deploy" / "worker" / "release-hive.sh").read_text()

    assert '--app-version "$version"' in workflow
    assert "workflows: [Swarm worker image]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "release-hive.sh" in (ROOT / ".github" / "workflows" / "swarm-node-image.yml").read_text()
    assert 'ne (toString .Chart.AppVersion) "dev"' in template
    assert '"$image:$version"' in publish
    assert 'merge (dict "tag" (toString .Chart.AppVersion)) $workers.image' in template
    assert '(dict "image" $hookImage)' in template
    assert '(dict "image" $workers.image)' not in template
    assert "sha256" not in template
