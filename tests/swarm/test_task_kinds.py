import pytest

from scripts.swarm import cli, prompt
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

KINDS = ("code", "ci", "ops", "troubleshoot", "tune", "research")


def build(**task):
    return prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "phase": "p1", **task})


def test_a_task_without_a_kind_gets_the_code_prompt():
    assert build() == build(kind="code")
    assert "agentihooks swarm sw done --pr <pr url>" in build()


def test_each_kind_gets_its_own_prompt():
    texts = {kind: build(kind=kind) for kind in KINDS}
    assert len(set(texts.values())) == len(KINDS)
    assert "done --pr <pr url>" in texts["ci"]
    for kind in ("ops", "tune"):
        assert '--command "<command>" --output "<its output>"' in texts[kind]
        assert "done --pr" not in texts[kind]
    assert '--root-cause "<cause>" --evidence "<what shows it>"' in texts["troubleshoot"]
    assert (
        "--fix <pr url>" in texts["troubleshoot"] and '--filed "<the follow up you proposed>"' in texts["troubleshoot"]
    )
    assert "task add" not in texts["troubleshoot"]
    assert "--finding <link>" in texts["research"]
    for text in texts.values():
        assert text.index("agentihooks ledger --slug sw --as engineer@a1b2c3-0001 leave") < text.index(
            "agentihooks swarm sw done"
        )


def test_the_prompt_carries_the_proof_contract():
    text = build(kind="tune", contract={"must": "p99 under 200 ms", "check": "the latency panel", "judge": "master"})
    assert "Proof contract" in text
    assert all(part in text for part in ("p99 under 200 ms", "the latency panel", "master"))


def _start(env, kind):  # noqa: F811
    store, ledger, _ = env
    ledger.rows["t1"]["kind"] = kind
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    return store, ledger


def test_an_ops_task_cannot_be_marked_done_without_command_evidence(env, monkeypatch, capsys):  # noqa: F811
    store, ledger = _start(env, "ops")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    assert run("sw", "done") == 1
    assert run("sw", "done", "--command", "kubectl get pods") == 1
    assert "--output" in capsys.readouterr().err
    assert ledger.rows["t1"]["state"] != "done"
    assert [a.state for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001"] != ["finished"]
    assert run("sw", "done", "--command", "kubectl get pods", "--output", "cache-0 Running") == 0
    assert ledger.rows["t1"]["state"] == "done"
    assert ledger.rows["t1"]["proof"] == {"command": "kubectl get pods", "output": "cache-0 Running"}


@pytest.mark.parametrize(
    "finding",
    ["my notes", "See https://example.com/artifact", "https://example.com/artifact explains the result", ""],
)
def test_research_done_refusal_explains_the_single_artifact_link(env, monkeypatch, capsys, finding):  # noqa: F811
    store, ledger = _start(env, "research")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    capsys.readouterr()

    assert cli.main(["sw", "done", "--finding", finding]) == 1

    assert capsys.readouterr().err == (
        "swarm: a research task is done only with its proof: "
        "--finding must be a single link to the artifact; put prose in a task comment\n"
    )
    assert ledger.rows["t1"]["state"] == "claimed"
    assert next(a for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001").state != "finished"


def test_troubleshoot_and_research_close_with_their_own_proof(env, monkeypatch):  # noqa: F811
    _, ledger = _start(env, "troubleshoot")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    assert run("sw", "done", "--root-cause", "a stale lock", "--evidence", "the lock age in the log") == 1
    args = ("--root-cause", "a stale lock", "--evidence", "the lock age in the log", "--filed", "t9")
    assert run("sw", "done", *args) == 0
    assert ledger.rows["t1"]["proof"]["filed"] == "t9"
    ledger.rows["t2"].update(kind="research", state="claimed")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "ci@a1b2c3-0001")
    assert run("sw", "done", "--finding", "my notes") == 1
    assert run("sw", "done", "--finding", "https://github.com/o/r/issues/4#issuecomment-1") == 0


@pytest.mark.parametrize("kind", KINDS)
def test_the_issue_step_applies_only_where_the_repo_has_issues(kind):
    text = build(kind=kind)
    assert "hasIssuesEnabled" in text
    assert "the ledger task is the spec" in text
    assert "1. Open a GitHub issue" not in text


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_code_kinds_activate_serena_on_the_worktree(kind):
    text = build(kind=kind)
    assert "mcp__serena__activate_project" in text
    assert "replace_symbol_body" in text


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_a_ci_agent_proposes_a_further_bottleneck_as_a_follow_up_and_never_queues_a_task(kind):
    text = prompt.build("sw", "/repo", "ci", "ci@a1b2c3-0001", {"id": "t1", "title": "x", "phase": "p1", "kind": kind})
    assert (
        "Propose a further bottleneck as a follow up: agentihooks ledger --slug sw --as ci@a1b2c3-0001 followup add"
        in text
    )
    assert "task add" not in text
