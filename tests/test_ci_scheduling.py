from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def test_push_and_other_non_dispatch_runs_each_keep_their_own_group():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    suffix = " || format('tests-{0}-{1}', github.event_name, github.event.pull_request.number || github.run_id) }}"
    group = workflow["concurrency"]["group"]
    assert group.endswith(suffix)
    assert "github.event_name == 'push'" not in group.removesuffix(suffix)


def test_only_pull_request_updates_cancel_running_checks():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
