from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_queue_suites_use_a_precise_reuse_condition():
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    assert (
        jobs["unit"].get("if")
        == "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}"
    )
