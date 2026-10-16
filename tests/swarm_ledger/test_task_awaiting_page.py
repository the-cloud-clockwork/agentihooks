import json
import subprocess

import pytest

from tests.swarm_ledger.test_task_proof_page import PLAIN, function_source, nodes, render


def label(task):
    script = (
        "const h = (tag, attrs) => ({ tag, ...attrs });"
        + function_source("awaitingLabel")
        + f"process.stdout.write(JSON.stringify(awaitingLabel({json.dumps(task)}) ?? null));"
    )
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


@pytest.mark.parametrize(
    ("awaiting", "text"),
    [("recovery_pending", "recovery pending"), ("merge", "merge"), ("approval", "approval"), ("fix", "fix")],
)
def test_a_task_row_shows_its_awaiting_state(awaiting, text):
    (shown,) = nodes(render({**PLAIN, "awaiting": awaiting})["tree"], "awaiting")
    assert shown[2] == text


def test_the_awaiting_label_names_what_the_task_waits_on():
    assert label({"awaiting": "recovery_pending"}) == {"tag": "span", "class": "awaiting", "text": "recovery pending"}


@pytest.mark.parametrize(
    "task",
    [PLAIN, {**PLAIN, "awaiting": ""}, {**PLAIN, "awaiting": None}, {**PLAIN, "state": "done", "awaiting": "merge"}],
)
def test_a_task_row_without_an_awaiting_state_shows_none(task):
    assert label(task) is None
    assert nodes(render(task)["tree"], "awaiting") == []
