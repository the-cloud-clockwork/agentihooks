import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
PROOF_BRANCH = "diffcheck/plant"
_TOKENS = {"**": "(?:.*)", "*": "(?:[^/]*)", "?": "?", "+": "+"}


def _matches(pattern, branch):
    parts = re.findall(r"\*\*|[*?+]|\[[^\]]*\]|[^*?+\[]+", pattern)
    assert "".join(parts) == pattern, pattern
    regex = "".join(_TOKENS.get(part) or (part if part.startswith("[") else re.escape(part)) for part in parts)
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


def _events(workflow):
    events = workflow.get(True, workflow.get("on"))
    return dict.fromkeys([events] if isinstance(events, str) else events) if isinstance(events, (str, list)) else events


def _push_filters():
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        events = _events(yaml.safe_load(path.read_text()))
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
        ({"tags-ignore": ["v*"]}, False),
        ({"branches": ["diffchecks?/**"]}, True),
        ({"branches": ["dif+check/**"]}, True),
        ({"branches": ["[cd]iffcheck/**"]}, True),
        ({"branches": ["[xy]iffcheck/**"]}, False),
        ({"branches": ["**+plant"]}, True),
        ({"branches": ["diffcheck/*?plant"]}, True),
    ],
)
def test_the_filter_reader_follows_github_branch_filters(push, runs):
    assert _runs_on(push, PROOF_BRANCH) is runs


@pytest.mark.parametrize("text", ["on: push\n", "on: [push, pull_request]\n", "on:\n  push:\n"])
def test_every_form_of_on_yields_its_push_trigger(text):
    assert "push" in _events(yaml.safe_load(text))


def test_no_push_triggered_workflow_runs_on_a_proof_branch():
    pushed = dict(_push_filters())
    assert pushed
    assert [name for name, push in pushed.items() if _runs_on(push, PROOF_BRANCH)] in ([], ["branch-lanes.yml"])
