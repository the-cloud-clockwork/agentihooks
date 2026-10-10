from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def test_push_concurrency_is_per_run():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert workflow["concurrency"]["group"].endswith(
        "|| format('tests-{0}-{1}', github.event_name, github.event.pull_request.number || github.run_id) }}"
    )


def test_only_pull_request_updates_cancel_running_checks():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_sonar_uses_a_hosted_runner():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert workflow["jobs"]["sonar"]["runs-on"] == "ubuntu-latest"
