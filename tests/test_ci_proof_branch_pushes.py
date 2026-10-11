import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
PROOF_BRANCH = "diffcheck/plant"


def _matches(pattern, branch):
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", ".")
    return re.fullmatch(regex, branch) is not None


def _runs_on(push, branch):
    if "branches-ignore" in push:
        return not any(_matches(pattern, branch) for pattern in push["branches-ignore"])
    if "branches" not in push:
        return "tags" not in push and "tags-ignore" not in push
    runs = False
    for pattern in push["branches"]:
        if _matches(pattern.removeprefix("!"), branch):
            runs = not pattern.startswith("!")
    return runs


def _push_filters():
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get(True, workflow.get("on"))
        if isinstance(events, (str, list)):
            events = dict.fromkeys([events] if isinstance(events, str) else events)
        if "push" in events:
            yield path.name, events["push"] or {}


@pytest.mark.parametrize(
    ("push", "runs"),
    [
        ({}, True),
        ({"branches": ["diffcheck/**"]}, True),
        ({"branches": ["dev", "diffcheck/**"]}, True),
        ({"branches": ["diffcheck/*"]}, True),
        ({"branches": ["**", "!diffcheck/**"]}, False),
        ({"branches": ["*"]}, False),
        ({"branches": ["dev"]}, False),
        ({"branches-ignore": ["dev", "main"]}, True),
        ({"branches-ignore": ["dev", "diffcheck/**"]}, False),
        ({"tags": ["v*"]}, False),
    ],
)
def test_the_filter_reader_follows_github_branch_filters(push, runs):
    assert _runs_on(push, PROOF_BRANCH) is runs


def test_no_push_triggered_workflow_runs_on_a_proof_branch():
    pushed = dict(_push_filters())
    assert pushed
    assert [name for name, push in pushed.items() if _runs_on(push, PROOF_BRANCH)] == []
