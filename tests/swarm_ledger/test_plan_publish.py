import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_artifacts, ledger_publish, ledger_tasks, ledger_workspace
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_kind
from tests.swarm_ledger.test_plan_kind import add, update

plan_ledger = test_plan_kind.plan_ledger

PLAN = "https://github.com/acme/app/issues/12"


def phase_plan(slug, url=PLAN, phase="p1"):
    op = {"op": "phase_update", "id": f"plan-{phase}", "by": "planner", "item": f"phases/{phase}"}
    op["fields"] = {"plan_url": url}
    core.check_op(op)
    return core.sync(slug, ops=[op])


def task(state, task_id):
    return next(t for t in state["tasks"] if t["id"] == task_id)


def test_phase_records_a_plan_link_and_refuses_anything_else(plan_ledger):
    state, _ = phase_plan(plan_ledger)
    assert state["phases"][0]["plan_url"] == PLAN
    with pytest.raises(ValueError, match="plan_url must be an http or https link"):
        phase_plan(plan_ledger, url="issue twelve")


def test_task_added_to_a_phase_with_a_plan_carries_its_link(plan_ledger):
    add(plan_ledger, "before")
    phase_plan(plan_ledger)
    state, _ = add(plan_ledger, "after")
    assert "plan_url" not in task(state, "before")
    assert task(state, "after")["plan_url"] == PLAN


def test_explicit_plan_link_wins_over_the_phase_and_can_be_set_later(plan_ledger):
    phase_plan(plan_ledger)
    other = "https://github.com/acme/app/issues/13"
    state, _ = add(plan_ledger, "own", plan_url=other)
    assert task(state, "own")["plan_url"] == other
    op = {"op": "task_update", "id": "link-own", "by": "planner", "item": "tasks/own", "fields": {"plan_url": PLAN}}
    core.check_op(op)
    state, _ = core.sync(plan_ledger, ops=[op])
    assert task(state, "own")["plan_url"] == PLAN
    with pytest.raises(ValueError, match="plan_url must be an http or https link"):
        add(plan_ledger, "bad", plan_url="the plan")


def test_slice_is_refused_while_a_slice_task_carries_no_plan_link(plan_ledger):
    add(plan_ledger, "plan", lane="plan", kind="plan")
    add(plan_ledger, "build")
    state, _ = update(plan_ledger, state="done", proof={"slice": "build"})
    assert task(state, "plan")["state"] == "open"
    assert any("publish the plan" in w for w in state["_meta"]["warnings"])
    phase_plan(plan_ledger)
    add(plan_ledger, "ship")
    state, _ = update(plan_ledger, state="done", proof={"slice": "ship"})
    assert task(state, "plan")["state"] == "done"


def test_steering_names_the_plan_link():
    text = ledger_workspace.steering({"id": "t1", "title": "Build", "plan_url": PLAN})
    assert f"Plan: {PLAN}" in text


def test_plan_artifact_counts_as_requested():
    doc = {"tasks": [{"id": "plan", "lane": "plan"}], "artifacts": []}
    file = {"id": f"{'a' * 64}.md", "type": "text/markdown", "size": 4}
    op = {"op": "artifact_add", "id": "art-1", "by": "planner", "task": "plan", "title": "Plan", "file": file}
    ctx = SimpleNamespace(meta={"members": {"planner": {}}}, refused=[], at=1, stamp=lambda *a: None)
    ctx.record = lambda *a, **k: None
    assert ledger_artifacts.apply(doc, dict(op), ctx) is False
    assert ctx.refused == [ledger_artifacts.REFUSED]
    ledger_artifacts.check({**op, "plan": True})
    assert ledger_artifacts.apply(doc, {**op, "plan": True}, ctx) is True
    assert doc["artifacts"][0]["plan"] is True
    with pytest.raises(ValueError):
        ledger_artifacts.check({**op, "plan": "yes"})


def gh(issues, created=PLAN):
    calls = []

    def run(argv, **_):
        calls.append(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            if issues is None:
                return subprocess.CompletedProcess(argv, 1, "", "not a git repository")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"hasIssuesEnabled": issues}), "")
        return subprocess.CompletedProcess(argv, 0, f"Creating issue\n{created}\n", "")

    return run, calls


@pytest.mark.parametrize("issues", [False, None])
def test_publish_goes_to_an_artifact_without_issues(issues):
    run, calls = gh(issues)
    uploads = []

    def artifact(path, title):
        uploads.append((path, title))
        return "http://127.0.0.1:8765/artifacts/demo/x.md"

    url, where = ledger_publish.publish("plan.md", "Slice plan", "", artifact, run)
    assert (url, where) == ("http://127.0.0.1:8765/artifacts/demo/x.md", "artifact")
    assert uploads == [("plan.md", "Slice plan")]
    assert len(calls) == 1


def test_publish_opens_an_issue_where_the_repo_has_issues():
    run, calls = gh(True)
    url, where = ledger_publish.publish("plan.md", "Slice plan", "acme/app", lambda *a: pytest.fail("no artifact"), run)
    assert (url, where) == (PLAN, "issue")
    assert calls[0] == ["gh", "repo", "view", "acme/app", "--json", "hasIssuesEnabled"]
    assert calls[1] == [
        "gh",
        "issue",
        "create",
        "--title",
        "Slice plan",
        "--body-file",
        "plan.md",
        "--repo",
        "acme/app",
    ]


def test_publish_stops_when_the_issue_cannot_be_opened():
    def run(argv, **_):
        if argv[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(argv, 0, '{"hasIssuesEnabled": true}', "")
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")

    with pytest.raises(ledger_publish.PublishError, match="HTTP 403"):
        ledger_publish.publish("plan.md", "Slice plan", "", lambda *a: "", run)


def test_title_is_the_first_heading_or_names_the_phases():
    assert ledger_publish.title_of("intro\n# Build the page\n## Steps", ["p1"]) == "Build the page"
    assert ledger_publish.title_of("no heading", ["p1", "p2"]) == "Plan for phases p1, p2"


def test_publish_plan_command_links_each_phase_and_comments_it(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text("# Build the page\n\nSteps\n", encoding="utf-8")
    sent = []
    monkeypatch.setattr(ledger.ledger_publish, "publish", lambda path, title, repo, artifact: (PLAN, "issue"))
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: sent.extend(ops or []) or {"rejected": []})
    monkeypatch.setattr(
        "sys.argv", ["ledger", "--slug", "demo", "--as", "planner", "publish-plan", str(plan), "--phase", "p1,p2"]
    )
    ledger.main()
    updates = [o for o in sent if o["op"] == "phase_update"]
    comments = [o for o in sent if o["op"] == "add"]
    assert [(o["item"], o["fields"]) for o in updates] == [
        ("phases/p1", {"plan_url": PLAN}),
        ("phases/p2", {"plan_url": PLAN}),
    ]
    assert [o["thread"] for o in comments] == ["phases/p1/comments", "phases/p2/comments"]
    assert all(PLAN in o["text"] for o in comments)
    for o in sent:
        core.check_op(o)
    assert json.loads(capsys.readouterr().out) == {"plan_url": PLAN, "published_to": "issue", "phases": ["p1", "p2"]}


def test_task_cli_passes_an_explicit_plan_link(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append((kind, fields)))
    monkeypatch.setattr(
        "sys.argv", ["ledger", "--slug", "demo", "--as", "master", "task", "add", "t9", "Build", "--plan", PLAN]
    )
    ledger.main()
    assert sent[0][1]["plan_url"] == PLAN
    assert "plan_url" in ledger_tasks.UPDATABLE


def test_ledger_page_shows_the_plan_link_on_a_task():
    page = (Path(ledger.__file__).parent / "template.html").read_text(encoding="utf-8")
    assert '"pr_url", "plan_url", "done"' in page
    assert 'link(item.plan_url, "plan")' in page
