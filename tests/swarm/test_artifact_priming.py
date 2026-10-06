from pathlib import Path

import pytest

from scripts.swarm import prompt


@pytest.mark.parametrize("lane", ["eng", "ci", "master"])
def test_agents_are_told_to_publish_artifacts_as_they_are_produced(lane):
    text = prompt.build("demo", "/repo", lane, "agent", {"id": "one", "title": "One"})

    assert (
        "Publish plans, screenshots, reports and proof files as they are produced with "
        'agentihooks ledger --slug demo --as agent artifact <file> "<title in plain words>": '
        "markdown, JSON, SVG or an image for operator review; it opens rendered "
        "from the artifacts icon on the ledger page."
    ) in text


def test_packaged_toolbelt_tells_agents_to_publish_produced_artifacts():
    rule = Path(__file__).resolve().parents[2] / "profiles/package/rules/agentihooks-toolbelt.md"
    text = rule.read_text()

    assert "Publish plans, screenshots, reports and proof files as they are produced" in text
    assert 'agentihooks ledger --slug <slug> --as <name> artifact <file> "<title in plain words>"' in text
