import pytest

from scripts.swarm import done_gate, prompt
from scripts.swarm.ledger_events import PullRequest
from tests.swarm.test_cli import env, run  # noqa: F401

URL = "https://github.com/o/r/pull/9"
NAME = "engineer@a1b2c3-0001"
PROOF = ("--command", "probe", "--output", "before 3 after 0")
REFUSED = (
    f"pull request {URL} is open, not merged; release the seat with swarm ready <handoff doc> so the swarm "
    "merges it, or give --filed when the fix is proposed instead of made"
)
READY = (
    "If this work opens a pull request, record it with agentihooks swarm sw pr <pr url>. Once its review closed "
    "and it is out of draft, write a Handoff v2 document, leave the crew with "
    f"agentihooks ledger --slug sw --as {NAME} leave and release your seat with agentihooks swarm sw ready "
    "<handoff doc> instead of done: the swarm merges it and reopens the task for its proof. Done refuses while a "
    "recorded pull request is open, unless the proof gives --filed for a fix proposed instead of made."
)


def reader(state):
    seen = []

    def read(url):
        seen.append(url)
        return PullRequest(state, None, 1, False)

    read.seen = seen
    return read


@pytest.mark.parametrize("kind", ["ops", "tune", "troubleshoot", "research"])
@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_a_non_code_task_with_an_unmerged_recorded_pull_request_is_refused(kind, state):
    read = reader(state)
    assert done_gate.refusal({"kind": kind}, URL, read) == REFUSED.replace("open,", f"{state.lower()},")
    assert read.seen == [URL]


@pytest.mark.parametrize("kind", ["ops", "tune", "troubleshoot", "research"])
def test_a_non_code_task_that_files_its_fix_never_reads_github(kind):
    read = reader("OPEN")
    assert done_gate.refusal({"kind": kind, "proof": {"filed": "f1"}}, URL, read) == ""
    assert done_gate.refusal({"kind": kind, "proof": {"filed": " "}}, URL, read) == REFUSED
    assert read.seen == [URL]


def test_a_non_code_task_without_a_pull_request_never_reads_github():
    read = reader("OPEN")
    assert done_gate.refusal({"kind": "tune"}, "", read) == ""
    assert read.seen == []


def test_a_merged_pull_request_closes_a_non_code_task():
    assert done_gate.refusal({"kind": "tune"}, URL, reader("MERGED")) == ""


def test_a_code_task_keeps_its_own_refusal_and_ignores_filed():
    refused = f"pull request {URL} is open, not merged; merge it, then run swarm done again"
    assert done_gate.refusal({"kind": "code", "proof": {"filed": "f1"}}, URL, reader("OPEN")) == refused


def _tune(env, monkeypatch, state):  # noqa: F811
    _, ledger, _ = env
    ledger.rows["t1"].update(kind="tune", pr_url=URL)
    ledger.pulls[URL] = PullRequest(state, None, 1, False)
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", NAME)
    return ledger


def test_swarm_done_refuses_a_tune_task_while_its_recorded_pull_request_is_open(env, monkeypatch, capsys):  # noqa: F811
    ledger = _tune(env, monkeypatch, "OPEN")
    capsys.readouterr()
    assert run("sw", "done", *PROOF) == 1
    assert capsys.readouterr().err == f"swarm: {REFUSED}\n"
    assert ledger.rows["t1"]["state"] != "done"


def test_swarm_done_closes_a_tune_task_whose_proof_files_the_fix(env, monkeypatch):  # noqa: F811
    ledger = _tune(env, monkeypatch, "OPEN")
    assert run("sw", "done", *PROOF, "--filed", "f1") == 0
    assert ledger.rows["t1"]["state"] == "done"
    assert ledger.rows["t1"]["proof"]["filed"] == "f1"


def test_swarm_done_closes_a_tune_task_whose_pull_request_merged(env, monkeypatch):  # noqa: F811
    ledger = _tune(env, monkeypatch, "MERGED")
    assert run("sw", "done", *PROOF) == 0
    assert ledger.rows["t1"]["state"] == "done"


def build(**task):
    return prompt.build("sw", "/repo", "eng", NAME, {"id": "t1", "title": "x", "phase": "p1", **task})


@pytest.mark.parametrize("kind", ["ops", "tune", "troubleshoot", "research"])
def test_a_non_code_prompt_names_ready_for_a_pull_request_before_the_closing_step(kind):
    text = build(kind=kind, pr_url=URL)
    assert READY in text
    assert text.index(READY) < text.index("agentihooks swarm sw done")


@pytest.mark.parametrize("kind", ["code", "ci", "plan"])
def test_code_and_plan_prompts_are_unchanged(kind):
    assert READY not in build(kind=kind)
