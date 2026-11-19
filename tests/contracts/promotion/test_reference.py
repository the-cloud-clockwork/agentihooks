import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.feature("promotion-reference-timings")
@pytest.mark.parametrize(
    ("pattern", "source"),
    [
        (r"with a (\d+) minute timeout\.", "timeout"),
        (r"so the (\d+) minute job timeout holds", "timeout"),
        (r"reporting budget; the job timeout is (\d+) minutes", "timeout"),
        (r"for up to (\d+) minutes", "wait"),
        (r"within (\d+) minutes \| The job fails", "wait"),
    ],
)
def test_promotion_reference_timings_match_the_workflow_and_script(pattern: str, source: str) -> None:
    documentation = (ROOT / "docs/reference/promotion.md").read_text()
    if source == "timeout":
        workflow = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())
        expected = workflow["jobs"]["promote"]["timeout-minutes"]
    else:
        script = (ROOT / ".github/scripts/promote.cjs").read_text()
        wait = re.search(r"const deadline = now\(\) \+ (\d+) \* 60 \* 1000;", script)
        assert wait is not None, "The promotion mergeability wait must be read from its deadline"
        expected = int(wait[1])
    assert re.findall(pattern, documentation) == [str(expected)]
