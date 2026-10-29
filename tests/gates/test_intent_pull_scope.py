import hashlib
import json

import pytest

from scripts.gates import intent
from tests.gates.test_intent import DOC, PR, URL, chunk_classifier, planned  # noqa: F401

pytestmark = pytest.mark.unit

OTHER = "https://github.com/o/r/pull/10"
SCOPE = "tests that accept both gate states"


def declared(doc, url=URL, kind="prerequisite", scope=SCOPE):
    task = {**doc["tasks"][0], "pull_scopes": {url: {"kind": kind, "scope": scope}}}
    return {**doc, "tasks": [task]}


@pytest.mark.parametrize("kind", ["stage", "prerequisite"])
def test_a_declared_pull_request_is_judged_against_its_declared_scope(planned, kind):  # noqa: F811
    doc = declared(planned, kind=kind)
    state = intent.state_of(doc, doc["tasks"][0], PR)
    assert state["task_text"] == (
        f"This pull request is a declared {kind} of this task and delivers only: {SCOPE}. "
        "The whole task is judged at its final pull request."
    )
    assert "plan_lines" not in state
    assert "plan_chunk" not in state
    assert list(intent.questions_for(state)) == list(intent.BASE_QUESTIONS)
    assert intent.judge(state, decide=chunk_classifier()) == (
        "pass",
        "the phase can use it as delivered at probability 0.90",
    )


def test_an_undeclared_pull_request_is_judged_against_the_whole_task(planned):  # noqa: F811
    doc = declared(planned, url=OTHER)
    state = intent.state_of(doc, doc["tasks"][0], PR)
    assert state == intent.state_of(planned, planned["tasks"][0], PR)
    assert state["task_text"] == planned["tasks"][0]["description"]
    assert (state["plan_lines"], state["plan_chunk"]) == ("15-17", "line 15\nline 16\nline 17\n")


def test_a_changed_declaration_is_judged_again(planned):  # noqa: F811
    task = planned["tasks"][0]
    first, second = declared(planned), declared(planned, scope="the gate itself")
    assert intent._fingerprint(first, first["tasks"][0]) != intent._fingerprint(second, second["tasks"][0])
    other = declared(planned, url=OTHER)
    assert intent._fingerprint(other, other["tasks"][0]) == intent._fingerprint(planned, task)
    phase = planned["phases"][0]
    parent = [task["title"], task["description"], phase["title"], phase["description"]]
    chunk = {"plan_lines": "15-17", "plan_chunk": "line 15\nline 16\nline 17\n"}
    today = hashlib.sha256(json.dumps([*parent, chunk]).encode()).hexdigest()
    assert intent._fingerprint(planned, task) == today


def test_the_stamped_parent_intent_names_the_declared_scope():
    doc = declared(DOC, kind="stage")
    task = {**doc["tasks"][0], "pr_url": OTHER}
    assert intent.section(doc, task) == intent.section(DOC, DOC["tasks"][0])
    stamped = intent.section(doc, doc["tasks"][0])
    assert stamped == intent.section(DOC, DOC["tasks"][0]).replace(
        DOC["tasks"][0]["description"], f"declared stage: {SCOPE}"
    )
    assert DOC["tasks"][0]["description"] not in stamped


def test_the_body_stamp_reads_the_declaration_of_the_stamped_pull_request():
    doc = declared(DOC, url=OTHER, kind="stage")
    sent = []

    def run(args, input=None, **_):
        sent.append((args, input))
        return type("Done", (), {"returncode": 0, "stdout": '{"body": ""}'})()

    assert intent.stamp_body(OTHER, doc, doc["tasks"][0], run=run)
    (read, _), (patch, body) = sent
    assert read == ["gh", "pr", "view", OTHER, "--json", "body"]
    assert patch == ["gh", "api", "--method", "PATCH", "repos/o/r/pulls/10", "--input", "-"]
    stamped = intent.section(doc, {**doc["tasks"][0], "pr_url": OTHER})
    assert json.loads(body) == {"body": stamped}
    assert f"Intent check:** declared stage: {SCOPE}" in stamped
