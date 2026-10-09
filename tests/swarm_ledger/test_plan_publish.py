import argparse
import functools
import json
import re
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import (
    ledger,
    ledger_artifacts,
    ledger_phases,
    ledger_plans,
    ledger_publish,
    ledger_tasks,
    ledger_workspace,
)
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_kind
from tests.swarm_ledger.ledger_page import page_source
from tests.swarm_ledger.plan_slices import anchored
from tests.swarm_ledger.test_plan_kind import add, update

plan_ledger = test_plan_kind.plan_ledger

PLAN = "https://github.com/acme/app/issues/12"

UNMARKED = (
    "plan task sections need a slice marker above each heading, written as <!-- slice: name --> on its own line: {}"
)


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
    add(plan_ledger, "check")
    state, rejected = update(plan_ledger, state="done", proof={"slice": "build,check"})
    assert rejected == ["finish-plan"]
    assert task(state, "plan")["state"] == "open"
    assert state["_meta"]["warnings"] == [
        f"tasks/plan slice tasks carry no plan link: build, check. {ledger_tasks.PUBLISH}"
    ]
    phase_plan(plan_ledger)
    anchored(plan_ledger, "ship")
    add(plan_ledger, "ship", plan_slice="ship")
    state, rejected = update(plan_ledger, state="done", proof={"slice": "ship"})
    assert rejected == []
    assert task(state, "plan")["state"] == "done"


def test_steering_names_the_plan_link():
    text = ledger_workspace.steering({"id": "t1", "title": "Build", "plan_url": PLAN})
    assert text == f"# t1: Build\n\nPlan: {PLAN}\n"
    assert ledger_workspace.steering({"id": "t1", "title": "Build"}) == "# t1: Build\n"


def test_phase_plan_link_check_names_the_rule():
    ledger_phases.check_fields({"plan_url": PLAN})
    ledger_phases.check_fields({"title": "No link"})
    for bad in (5, "", "ftp://acme/plan", "https://acme/a plan", "see https://acme/plan"):
        with pytest.raises(ValueError) as raised:
            ledger_phases.check_fields({"plan_url": bad})
        assert str(raised.value) == "plan_url must be an http or https link"


def test_task_add_without_phases_in_the_ledger_takes_only_its_own_link():
    ctx = SimpleNamespace(refused=[], record=lambda *a, **k: None)
    op = {"op": "task_add", "id": "a", "by": "swarm", "task": "t1", "title": "Build", "lane": "eng", "phase": "p1"}
    doc = {"tasks": []}
    assert ledger_tasks._add(doc, dict(op), ctx) is True
    assert "plan_url" not in doc["tasks"][0]
    doc = {"tasks": []}
    assert ledger_tasks._add(doc, {**op, "plan_url": PLAN}, ctx) is True
    assert doc["tasks"][0]["plan_url"] == PLAN


def test_a_move_into_an_anchored_phase_is_judged_on_the_moved_task(monkeypatch):
    from scripts.swarm_ledger import plan_ranges

    monkeypatch.setattr(plan_ranges, "anchors", lambda doc, phase: ["first"] if phase.get("id") == "p1" else [])
    doc = {
        "phases": [{"id": "p1"}, {"id": "p2"}],
        "tasks": [{"id": "other", "phase": "p1"}, {"id": "mine", "phase": "p2"}, {"id": "home", "phase": "p1"}],
    }

    def move(item, by="planner"):
        return {"op": "task_update", "by": by, "item": f"tasks/{item}", "fields": {"phase": "p1"}}

    refused = "phase p1 has a plan with slice anchors"
    assert ledger_tasks.update_refusal(doc, move("mine")).startswith(refused)
    assert ledger_tasks.update_refusal(doc, move("ghost")).startswith(refused)
    assert ledger_tasks.update_refusal(doc, move("home")) == ""
    assert ledger_tasks.update_refusal(doc, move("mine", by="swarm")) == ""
    assert ledger_tasks.unsliced_refusal({}, {"phase": "p1"}, "planner") == ""


def test_artifact_check_takes_a_plan_flag_and_keeps_its_other_rules():
    file = {"id": f"{'a' * 64}.md"}
    op = {"op": "artifact_add", "id": "art-1", "by": "planner", "task": "plan", "title": "Plan", "file": file}
    ledger_artifacts.check(dict(op))
    ledger_artifacts.check({**op, "plan": True, "request": "m-1"})
    with pytest.raises(ValueError) as raised:
        ledger_artifacts.check({**op, "plan": False})
    assert str(raised.value) == "plan must be true, marking a published plan"
    for bad in ({"plan": 1}, {"by": "operator"}, {"by": 5}, {"by": "1bad"}, {"extra": 1}):
        with pytest.raises(ValueError):
            ledger_artifacts.check({**op, **bad})


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

    def run(argv, **kwargs):
        assert kwargs == {"capture_output": True, "text": True}
        calls.append(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            if issues is None:
                return subprocess.CompletedProcess(argv, 1, "", "fatal: not a git repository")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"hasIssuesEnabled": issues}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 0, f"Creating issue in acme/app\n\n{created}\n", "")

    return run, calls


class Tracker:
    def __init__(self):
        self.rows = []
        self.calls = []

    def run(self, argv, **kwargs):
        assert kwargs == {"capture_output": True, "text": True}
        self.calls.append(argv)
        verb = argv[2]
        if argv[1] == "repo":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"hasIssuesEnabled": True}), "")
        if verb == "list":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.rows), "")
        if verb == "create":
            url = f"https://github.com/acme/app/issues/{12 + len(self.rows)}"
            self.rows.append({"url": url, "state": "OPEN", "body": argv[argv.index("--body") + 1]})
            return subprocess.CompletedProcess(argv, 0, f"{url}\n", "")
        row = next(r for r in self.rows if r["url"] == argv[3])
        row["state"] = "CLOSED" if verb == "close" else "OPEN"
        return subprocess.CompletedProcess(argv, 0, "", "")

    def states(self):
        return [row["state"] for row in self.rows]


@pytest.mark.parametrize("issues", [False, None])
def test_publish_goes_to_an_artifact_without_issues(issues):
    run, calls = gh(issues)
    uploads = []

    def artifact(path, title):
        uploads.append((path, title))
        return "http://127.0.0.1:8765/artifacts/demo/x.md"

    url, where = ledger_publish.publish("plan.md", "Slice plan", "", artifact, run, issue_title="Slice plan")
    assert (url, where) == ("http://127.0.0.1:8765/artifacts/demo/x.md", "artifact")
    assert uploads == [("plan.md", "Slice plan")]
    assert len(calls) == 1


def test_publish_opens_an_issue_where_the_repo_has_issues():
    run, calls = gh(True)
    stored = "http://127.0.0.1:8765/artifacts/demo/x.md"
    url, where = ledger_publish.publish(
        "plan.md", "Slice plan", "acme/app", lambda *a: stored, run, issue_title="Slice plan"
    )
    assert (url, where) == (PLAN, "issue")
    assert calls[0] == ["gh", "repo", "view", "acme/app", "--json", "hasIssuesEnabled"]
    assert calls[1] == [
        "gh",
        "issue",
        "list",
        "--state",
        "all",
        "--search",
        f'"{stored}" in:body',
        "--json",
        "url,state,body",
        "--repo",
        "acme/app",
    ]
    assert calls[2] == [
        "gh",
        "issue",
        "create",
        "--title",
        "Slice plan",
        "--body",
        f"Slice plan\n\n{stored}",
        "--repo",
        "acme/app",
    ]


def test_publish_stops_when_gh_fails_for_another_reason_than_a_missing_repo():
    def run(argv, **_):
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 401: Bad credentials\n")

    with pytest.raises(ledger_publish.PublishError) as raised:
        ledger_publish.publish(
            "plan.md", "Slice plan", "", lambda *a: pytest.fail("no artifact"), run, issue_title="Slice plan"
        )
    assert str(raised.value) == "gh repo view failed: HTTP 401: Bad credentials"


def test_a_folder_without_a_github_remote_publishes_an_artifact():
    def run(argv, **_):
        message = "none of the git remotes configured for this repository point to a known GitHub host"
        return subprocess.CompletedProcess(argv, 1, "", message)

    assert ledger_publish.publish("plan.md", "Slice plan", "", lambda *a: "link", run, issue_title="Slice plan") == (
        "link",
        "artifact",
    )


def test_publish_opens_an_issue_in_the_current_repo_without_a_repo_name():
    run, calls = gh(True)
    published = ledger_publish.publish("plan.md", "Slice plan", "", lambda *a: "link", run, issue_title="Build")
    assert published == (PLAN, "issue")
    assert calls == [
        ["gh", "repo", "view", "--json", "hasIssuesEnabled"],
        ["gh", "issue", "list", "--state", "all", "--search", '"link" in:body', "--json", "url,state,body"],
        ["gh", "issue", "create", "--title", "Build", "--body", "Build\n\nlink"],
    ]


def test_publish_stops_when_the_issue_cannot_be_opened():
    def run(argv, **_):
        if argv[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(argv, 0, '{"hasIssuesEnabled": true}', "")
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")

    with pytest.raises(ledger_publish.PublishError, match="HTTP 403"):
        ledger_publish.publish("plan.md", "Slice plan", "", lambda *a: "", run, issue_title="Slice plan")


def test_title_is_the_first_heading_or_names_the_phases():
    assert ledger_publish.title_of("intro\n# Build the page\n## Steps", ["p1"]) == "Build the page"
    assert ledger_publish.title_of("no heading", ["p1", "p2"]) == "Plan for phases p1, p2"


def test_publish_plan_command_links_each_phase_and_comments_it(plan_ledger, tmp_path, monkeypatch, capsys):
    phase_two = {"op": "phase_add", "id": "add-p2", "by": "planner", "phase": "p2", "title": "Ship"}
    core.check_op(phase_two)
    core.sync(plan_ledger, ops=[phase_two])
    plan = tmp_path / "plan.md"
    plan.write_text("intro\n# Build the page\n## Build\nSteps\n## Ship\nShip it\n", encoding="utf-8")
    core.sync(plan_ledger, ops=[{"op": "join", "id": "join-planner", "by": "planner", "role": "member"}])
    seen = []

    def publish(path, title, repo, artifact, issue_title):
        seen.append((path, title, repo))
        assert issue_title == "Build, Ship"
        artifact(path, title)
        return PLAN, "issue"

    monkeypatch.setattr(ledger.ledger_publish, "publish", publish)
    file = ledger_artifacts.store(plan_ledger, "plan.md", plan.read_bytes())
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: file)
    cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1, p2", "--repo", "acme/app")
    assert seen == [(str(plan), "Build the page", "acme/app")]
    state = core.sync(plan_ledger)[0]
    stored = f"{ledger.BASE}/artifacts/{plan_ledger}/{file['id']}"
    assert [phase["plan_ref"] for phase in state["phases"]] == [
        {"artifact": stored, "lines": "3-4"},
        {"artifact": stored, "lines": "5-6"},
    ]
    for phase in state["phases"]:
        assert phase["plan_url"] == PLAN
        [comment] = phase["comments"]
        assert comment["text"] == f"Plan published as a GitHub issue: {PLAN}"
        assert comment["by"] == "planner"
        assert re.fullmatch(r"c-[0-9a-f]{10}", comment["id"])
    assert json.loads(capsys.readouterr().out) == {"plan_url": PLAN, "published_to": "issue", "phases": ["p1", "p2"]}


def served(slug, ops=None):
    state, rejected = core.sync(slug, ops=ops)
    return {**state, "rejected": rejected}


def cli(monkeypatch, slug, *argv):
    monkeypatch.setattr(ledger, "call", served)
    args = ledger.build_parser().parse_args(["--slug", slug, "--as", "planner", *argv])
    getattr(ledger, f"cmd_{args.command.replace('-', '_')}")(args)


def test_publish_plan_without_issues_stores_a_plan_artifact_for_the_planner_task(
    plan_ledger, tmp_path, monkeypatch, capsys
):
    add(plan_ledger, "plan", lane="plan", kind="plan")
    join = {"op": "join", "id": "join-planner", "by": "planner", "role": "member"}
    core.sync(plan_ledger, ops=[join])
    plan = tmp_path / "plan.md"
    plan.write_text("# Ignored heading\n", encoding="utf-8")
    file = ledger_artifacts.store(plan_ledger, "plan.md", plan.read_bytes())
    uploads = []
    monkeypatch.setattr(ledger.ledger_publish, "has_issues", lambda repo, run=None: False)
    monkeypatch.setattr(
        ledger, "upload_artifact", lambda slug, name, path, request: uploads.append((slug, name, path, request)) or file
    )
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "plan")
    cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1", "--title", "Slice plan")
    url = f"{ledger.BASE}/artifacts/{plan_ledger}/{file['id']}"
    assert uploads == [(plan_ledger, "planner", str(plan), {"task": "plan", "title": "Slice plan", "plan": True})]
    state = core.sync(plan_ledger)[0]
    [row] = state["artifacts"]
    assert (row["title"], row["plan"], row["task"], row["file"]["id"]) == ("Slice plan", True, "plan", file["id"])
    assert state["phases"][0]["plan_url"] == url
    assert [c["text"] for c in state["phases"][0]["comments"]] == [f"Plan published on the ledger: {url}"]
    assert json.loads(capsys.readouterr().out) == {"plan_url": url, "published_to": "artifact", "phases": ["p1"]}


def test_publish_plan_needs_a_phase(plan_ledger, monkeypatch):
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, plan_ledger, "publish-plan", "plan.md", "--phase", " , ")
    assert raised.value.code == "publish-plan needs --phase with the ids of the phases the plan fills"


def test_publish_plan_reads_the_plan_as_utf8(monkeypatch):
    reads = []

    class PlanFile:
        def __init__(self, path):
            self.path = path

        def read_text(self, encoding):
            reads.append((self.path, encoding))
            return "# Café plan\n"

    titles = []
    monkeypatch.setattr(ledger, "Path", PlanFile)
    stub_publish(monkeypatch, titles)
    monkeypatch.setattr(
        ledger, "call", lambda slug, ops=None: {"rejected": ["x"]} if ops else {"phases": [{"id": "p1", "title": "A"}]}
    )
    args = ledger.build_parser().parse_args(
        ["--slug", "s", "--as", "planner", "publish-plan", "plan.md", "--phase", "p1"]
    )
    with pytest.raises(SystemExit):
        ledger.cmd_publish_plan(args)
    assert reads == [("plan.md", "utf-8")]
    assert titles == ["Café plan"]


def test_publish_plan_exits_with_the_gh_failure(plan_ledger, tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n", encoding="utf-8")

    def fail(*_, **__):
        raise ledger.ledger_publish.PublishError("gh issue create failed: HTTP 403")

    monkeypatch.setattr(ledger.ledger_publish, "publish", fail)
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1")
    assert raised.value.code == "gh issue create failed: HTTP 403"


def test_publish_plan_refuses_an_unknown_phase_before_publishing(plan_ledger, tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n", encoding="utf-8")
    monkeypatch.setattr(ledger.ledger_publish, "publish", lambda *a, **k: pytest.fail("published"))
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "nope")
    assert raised.value.code == "publish-plan names an unknown phase"
    assert capsys.readouterr().out == ""


def test_publish_plan_refuses_a_multi_phase_plan_without_phase_headings(plan_ledger, tmp_path, monkeypatch):
    core.sync(plan_ledger, ops=[{"op": "phase_add", "id": "add-p2", "by": "planner", "phase": "p2", "title": "Ship"}])
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n## Build\nSteps\n", encoding="utf-8")
    monkeypatch.setattr(ledger.ledger_publish, "publish", lambda *a, **k: pytest.fail("published"))
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1,p2")
    assert raised.value.code == "plan needs one heading for phase Ship"


@pytest.mark.parametrize(
    ("text", "phases", "missing"),
    [
        ("# Plan\n## Build\n<!-- slice: first -->\n### First\nOne\n### Second\nTwo\n", "p1", "Second on line 6"),
        (
            "# Plan\n## Build\n### First\n## Ship\n<!-- slice: s -->\n### Pack\n### Send\n",
            "p1,p2",
            "First on line 3, Send on line 7",
        ),
        ("intro\n# Plan\n## First\n\n<!-- slice: two -->\n## Two\n", "p1", "First on line 3"),
        ("## First\nOne\n<!-- slice: two -->\n## Two\n", "p1", "First on line 1"),
        ("# Plan\n## Build the page\n### First\nSteps\n### Second\nSteps\n", "p1", "Build the page on line 2"),
        ("# Plan\n## First\nOne\n", "p1", "First on line 2"),
        ("# Plan\n## Build\n### First\nOne\n", "p1", "First on line 3"),
    ],
)
def test_publish_plan_refuses_task_sections_without_a_slice_marker(
    plan_ledger, tmp_path, monkeypatch, capsys, text, phases, missing
):
    core.sync(plan_ledger, ops=[{"op": "phase_add", "id": "add-p2", "by": "planner", "phase": "p2", "title": "Ship"}])
    plan = tmp_path / "plan.md"
    plan.write_text(text, encoding="utf-8")
    monkeypatch.setattr(ledger.ledger_publish, "publish", lambda *a, **k: pytest.fail("published"))
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", phases)
    assert raised.value.code == UNMARKED.format(missing)
    assert capsys.readouterr().out == ""
    assert "plan_url" not in core.sync(plan_ledger)[0]["phases"][0]


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        (
            "# Plan\n## Build\nIntro\n<!-- slice: first -->\n  \n### First\n#### Detail\n"
            "```\n### Not a heading\n```\n<!-- slice: second -->\n### Second\n",
            "2-12",
        ),
        ("# Plan\n<!-- slice: one -->\n## One\n", "1-3"),
        ("Plain steps\nwith no headings\n", "1-2"),
    ],
)
def test_publish_plan_takes_a_fully_marked_plan(plan_ledger, tmp_path, monkeypatch, capsys, text, lines):
    plan = tmp_path / "plan.md"
    plan.write_text(text, encoding="utf-8")
    core.sync(plan_ledger, ops=[{"op": "join", "id": "join-planner", "by": "planner", "role": "member"}])
    file = ledger_artifacts.store(plan_ledger, "plan.md", plan.read_bytes())
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: file)
    monkeypatch.setattr(
        ledger.ledger_publish,
        "publish",
        lambda path, title, repo, artifact, issue_title: (artifact(path, title), "artifact"),
    )
    cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1")
    url = f"{ledger.BASE}/artifacts/{plan_ledger}/{file['id']}"
    phase = core.sync(plan_ledger)[0]["phases"][0]
    assert phase["plan_ref"] == {"artifact": url, "lines": lines}
    assert json.loads(capsys.readouterr().out) == {"plan_url": url, "published_to": "artifact", "phases": ["p1"]}


def test_a_standalone_task_adds_without_a_plan_or_a_slice(plan_ledger):
    state, rejected = add(plan_ledger, "standalone")
    assert rejected == []
    added = task(state, "standalone")
    assert "plan_url" not in added and "plan_slice" not in added and "plan_lines" not in added


def stub_publish(monkeypatch, titles):
    def publish(path, title, repo, artifact, issue_title):
        titles.append(title)
        artifact(path, title)
        return PLAN, "issue"

    monkeypatch.setattr(ledger.ledger_publish, "publish", publish)
    monkeypatch.setattr(ledger.ledger_publish, "close_issue", lambda url: titles.append(f"closed {url}"))
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: {"id": f"{'d' * 64}.md"})


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"rejected": ["x"], "_meta": {"warnings": ["first", "second"]}}, "first; second"),
        ({"rejected": ["x"], "_meta": {}}, "rejected: ['x']"),
        ({"rejected": ["x"]}, "rejected: ['x']"),
    ],
)
def test_publish_plan_exits_with_the_ledger_warnings(tmp_path, monkeypatch, state, message):
    plan = tmp_path / "plan.md"
    plan.write_text("no heading\n## One\nfirst\n## Two\nsecond\n", encoding="utf-8")
    titles = []
    stub_publish(monkeypatch, titles)
    phases = {"phases": [{"id": "p1", "title": "One"}, {"id": "p2", "title": "Two"}]}
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: state if ops else phases)
    args = ledger.build_parser().parse_args(
        ["--slug", "s", "--as", "planner", "publish-plan", str(plan), "--phase", "p1,p2"]
    )
    with pytest.raises(SystemExit) as raised:
        ledger.cmd_publish_plan(args)
    assert raised.value.code == message
    assert titles == ["Plan for phases p1, p2", f"closed {PLAN}"]


def test_publish_plan_names_a_sent_op_the_ledger_refused_without_a_reason(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    plan.write_text("# Rollout\nfirst\n", encoding="utf-8")
    stub_publish(monkeypatch, [])
    phases = {"phases": [{"id": "p1", "title": "One"}]}
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: {"rejected": [ops[1]["id"]]} if ops else phases)
    args = ledger.build_parser().parse_args(
        ["--slug", "s", "--as", "planner", "publish-plan", str(plan), "--phase", "p1"]
    )
    with pytest.raises(SystemExit) as raised:
        ledger.cmd_publish_plan(args)
    assert raised.value.code == (
        "plan_add on the ledger refused without a reason from the server: "
        "check that the entry exists, that you may change it and that its text is not empty"
    )


def test_publish_plan_artifact_outside_a_swarm_task_names_no_task(plan_ledger, tmp_path, monkeypatch):
    core.sync(plan_ledger, ops=[{"op": "join", "id": "join-planner", "by": "planner", "role": "member"}])
    plan = tmp_path / "plan.md"
    plan.write_text("# Master plan\n", encoding="utf-8")
    file = ledger_artifacts.store(plan_ledger, "plan.md", plan.read_bytes())
    monkeypatch.setattr(ledger.ledger_publish, "has_issues", lambda repo, run=None: False)
    monkeypatch.setattr(ledger, "upload_artifact", lambda slug, name, path, request: file)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1")
    [row] = core.sync(plan_ledger)[0]["artifacts"]
    assert (row["task"], row["title"]) == ("", "Master plan")


def test_publish_plan_parser_names_its_options():
    parser = ledger.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    helps = {a.dest: a.help for a in commands["publish-plan"]._actions}
    assert (helps["phase"], helps["title"], helps["repo"]) == (
        "comma separated ids of the phases the plan fills",
        "default the plan's first heading",
        "OWNER/NAME for the issue; default the current repo",
    )
    args = parser.parse_args(["--slug", "s", "--as", "planner", "publish-plan", "plan.md", "--phase", "p1"])
    assert (args.path, args.phase, args.title, args.repo) == ("plan.md", "p1", "", "")
    with pytest.raises(SystemExit):
        parser.parse_args(["--slug", "s", "--as", "planner", "publish-plan", "plan.md"])
    plan = next(a for a in commands["task"]._actions if a.dest == "plan")
    assert plan.help == "link to the published plan; default the phase's plan link"
    assert parser.parse_args(["--slug", "s", "--as", "m", "task", "add", "t1", "Build"]).plan == ""


def test_task_cli_passes_an_explicit_plan_link(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append((kind, fields)))
    monkeypatch.setattr(
        "sys.argv", ["ledger", "--slug", "demo", "--as", "master", "task", "add", "t9", "Build", "--plan", PLAN]
    )
    ledger.main()
    assert sent[0][1]["plan_url"] == PLAN
    assert "plan_url" in ledger_tasks.UPDATABLE


def test_task_cli_forwards_each_set_option_and_names_the_plan_slice(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda *args, **fields: sent.append(fields))
    base = ["ledger", "--slug", "demo", "--as", "master", "task", "add", "t9", "Build"]
    options = ["--kind", "ops", "--profile", "frontend", "--rank", "high", "--difficulty", "S", "--plan-slice", "t9"]
    monkeypatch.setattr("sys.argv", [*base, *options])
    ledger.main()
    keys = ("kind", "profile", "rank", "difficulty", "plan_slice")
    assert {k: sent[0][k] for k in keys} == {
        "kind": "ops",
        "profile": "frontend",
        "rank": "high",
        "difficulty": "S",
        "plan_slice": "t9",
    }
    monkeypatch.setattr("sys.argv", base)
    ledger.main()
    assert not set(keys) & set(sent[1])
    parser = ledger.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    option = next(a for a in commands["task"]._actions if a.dest == "plan_slice")
    assert (option.default, option.help) == ("", "task slice anchor; computes its plan lines")


def test_ledger_page_shows_the_plan_link_on_a_task():
    page = page_source()
    assert '"pr_url", "plan_url", "done"' in page
    assert 'link(item.plan_url, "plan")' in page


ROLLOUT = (
    "# Rollout\n## Build\n<!-- slice: first -->\n### First\nOne\n<!-- slice: second -->\n### Second\n\n"
    "## Ship\n<!-- slice: launch -->\n### Launch\nGo\n"
)
REVISED = (
    "# Rollout\n## Build\n<!-- slice: first -->\n### First\nOne\nTwo\n<!-- slice: third -->\n### Third\n\n"
    "## Ship\n<!-- slice: launch -->\n### Launch\nGo\n"
)


def published(slug, tmp_path, monkeypatch, issues=None):
    core.sync(slug, ops=[{"op": "phase_add", "id": "add-p2", "by": "planner", "phase": "p2", "title": "Ship"}])
    core.sync(slug, ops=[{"op": "join", "id": "join-planner", "by": "planner", "role": "member"}])
    plan = tmp_path / "plan.md"
    plan.write_text(ROLLOUT, encoding="utf-8")
    file = ledger_artifacts.store(slug, "plan.md", plan.read_bytes())
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: file)
    if issues is None:
        monkeypatch.setattr(
            ledger.ledger_publish,
            "publish",
            lambda path, title, repo, artifact, issue_title: (artifact(path, title) and PLAN, "issue"),
        )
        monkeypatch.setattr(ledger.ledger_publish, "close_issue", lambda url: None)
    else:
        for name in ("publish", "close_issue"):
            real = getattr(ledger.ledger_publish, name)
            monkeypatch.setattr(ledger.ledger_publish, name, functools.partial(real, run=issues.run))
    cli(monkeypatch, slug, "publish-plan", str(plan), "--phase", "p1,p2")
    return file, f"plan-{file['id'][:12]}"


def test_publish_plan_creates_one_plan_its_phase_parents_and_a_slice_per_marker(plan_ledger, tmp_path, monkeypatch):
    file, plan_id = published(plan_ledger, tmp_path, monkeypatch)
    state = core.sync(plan_ledger)[0]
    stored = f"{ledger.BASE}/artifacts/{plan_ledger}/{file['id']}"
    assert state["plans"] == [{"id": plan_id, "title": "Rollout", "artifact": stored, "url": PLAN}]
    assert [(phase["id"], phase["plan"]) for phase in state["phases"]] == [
        ("p1", f"plans/{plan_id}"),
        ("p2", f"plans/{plan_id}"),
    ]
    assert state["slices"] == [
        {"id": f"{plan_id}.first", "phase": "phases/p1", "anchor": "first", "lines": "3-5"},
        {"id": f"{plan_id}.second", "phase": "phases/p1", "anchor": "second", "lines": "6-7"},
        {"id": f"{plan_id}.launch", "phase": "phases/p2", "anchor": "launch", "lines": "10-12"},
    ]
    assert state["tasks"] == []


def test_publishing_the_same_plan_again_keeps_one_plan_and_its_slices(plan_ledger, tmp_path, monkeypatch):
    published(plan_ledger, tmp_path, monkeypatch)
    before = core.sync(plan_ledger)[0]
    plan = tmp_path / "plan.md"
    cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1,p2")
    after = core.sync(plan_ledger)[0]
    assert (after["plans"], after["slices"]) == (before["plans"], before["slices"])


def test_a_task_added_with_a_plan_slice_lists_under_that_slice(plan_ledger, tmp_path, monkeypatch):
    _, plan_id = published(plan_ledger, tmp_path, monkeypatch)
    add(plan_ledger, "build", plan_slice="second")
    state, rejected = add(plan_ledger, "ship", phase="p2", plan_slice="launch")
    assert rejected == []
    assert (task(state, "build")["slice"], task(state, "build")["plan_lines"]) == (f"slices/{plan_id}.second", "6-7")
    assert (task(state, "ship")["slice"], task(state, "ship")["plan_lines"]) == (f"slices/{plan_id}.launch", "10-12")


def test_a_plan_slice_in_a_phase_without_a_plan_sets_no_slice(plan_ledger):
    anchored(plan_ledger, "one")
    state, rejected = add(plan_ledger, "lone", plan_slice="one")
    assert rejected == []
    assert task(state, "lone")["plan_slice"] == "one"
    assert "slice" not in task(state, "lone")


def test_publish_plan_refuses_a_slice_name_repeated_across_its_phases(plan_ledger, tmp_path, monkeypatch, capsys):
    core.sync(plan_ledger, ops=[{"op": "phase_add", "id": "add-p2", "by": "planner", "phase": "p2", "title": "Ship"}])
    plan = tmp_path / "plan.md"
    plan.write_text("## Build\n<!-- slice: one -->\n### A\n## Ship\n<!-- slice: one -->\n### B\n", encoding="utf-8")
    monkeypatch.setattr(ledger.ledger_publish, "publish", lambda *a, **k: pytest.fail("published"))
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1,p2")
    assert raised.value.code == "each slice marker needs its own name across the plan's phases: one"
    assert capsys.readouterr().out == ""


def test_republishing_a_revised_plan_moves_its_phases_and_reconciles_slices_and_tasks(
    plan_ledger, tmp_path, monkeypatch, capsys
):
    _, old = published(plan_ledger, tmp_path, monkeypatch)
    add(plan_ledger, "build", plan_slice="first")
    add(plan_ledger, "polish", plan_slice="second")
    add(plan_ledger, "ship", phase="p2", plan_slice="launch")
    capsys.readouterr()
    plan = tmp_path / "plan.md"
    plan.write_text(REVISED, encoding="utf-8")
    revised = ledger_artifacts.store(plan_ledger, "plan.md", plan.read_bytes())
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: revised)
    cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1,p2")
    new = ledger_plans.plan_id(revised["id"])
    state = core.sync(plan_ledger)[0]
    assert [row["id"] for row in state["plans"]] == [old, new]
    assert [phase["plan"] for phase in state["phases"]] == [f"plans/{new}"] * 2
    assert state["slices"] == [
        {"id": f"{new}.first", "phase": "phases/p1", "anchor": "first", "lines": "3-6"},
        {"id": f"{new}.third", "phase": "phases/p1", "anchor": "third", "lines": "7-8"},
        {"id": f"{new}.launch", "phase": "phases/p2", "anchor": "launch", "lines": "11-13"},
    ]
    parents = {t["id"]: [t.get(key) for key in ("slice", "plan_slice", "plan_lines")] for t in state["tasks"]}
    assert parents == {
        "build": [f"slices/{new}.first", "first", "3-6"],
        "polish": [None, None, None],
        "ship": [f"slices/{new}.launch", "launch", "11-13"],
    }
    assert [(e["by"], e["kind"], e["target"]) for e in state["_meta"]["events"] if e["kind"].startswith("slice ")] == [
        ("planner", "slice changed", "tasks/build"),
        ("planner", "slice cleared", "tasks/polish"),
        ("planner", "slice changed", "tasks/ship"),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "plan_url": PLAN,
        "published_to": "issue",
        "phases": ["p1", "p2"],
        "tasks": {"changed": ["build", "ship"], "cleared": ["polish"]},
    }


def test_a_refused_publish_leaves_no_plan_entry(plan_ledger, tmp_path, monkeypatch):
    refusal = ledger_plans.phase_refusal
    monkeypatch.setattr(
        ledger_plans, "phase_refusal", lambda doc, phase: "phase refused" if phase.get("plan") else refusal(doc, phase)
    )
    with pytest.raises(SystemExit) as raised:
        published(plan_ledger, tmp_path, monkeypatch)
    assert "phase refused" in raised.value.code
    state = core.sync(plan_ledger)[0]
    assert state.get("plans", []) == []
    assert [phase.get("plan") for phase in state["phases"]] == [None, None]
    assert [e["target"] for e in state["_meta"]["events"] if e["target"].startswith("plans/")] == []


def test_a_publish_with_one_phase_refused_changes_no_phase_slice_plan_or_task(plan_ledger, tmp_path, monkeypatch):
    _, old = published(plan_ledger, tmp_path, monkeypatch)
    add(plan_ledger, "build", plan_slice="first")
    before = core.sync(plan_ledger)[0]
    refusal = ledger_plans.phase_refusal
    monkeypatch.setattr(
        ledger_plans,
        "phase_refusal",
        lambda doc, phase: "phase p2 refused" if phase["id"] == "p2" else refusal(doc, phase),
    )
    plan = tmp_path / "plan.md"
    plan.write_text(REVISED, encoding="utf-8")
    revised = ledger_artifacts.store(plan_ledger, "plan.md", plan.read_bytes())
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: revised)
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, plan_ledger, "publish-plan", str(plan), "--phase", "p1,p2")
    assert raised.value.code == "phase p2 refused"
    after = core.sync(plan_ledger)[0]
    keys = ("plans", "phases", "slices", "tasks")
    assert [after[key] for key in keys] == [before[key] for key in keys]
    assert [row["id"] for row in after["plans"]] == [old]
    new = f"plans/{ledger_plans.plan_id(revised['id'])}"
    assert [e for e in after["_meta"]["events"] if e["target"] == new] == []


def refuse_plans(monkeypatch, refusing):
    refusal = ledger_plans.phase_refusal
    monkeypatch.setattr(
        ledger_plans,
        "phase_refusal",
        lambda doc, phase: "phase refused" if refusing and phase.get("plan") else refusal(doc, phase),
    )


def verbs(issues):
    return [argv[2] for argv in issues.calls if argv[1] == "issue"]


def test_a_refused_publish_closes_its_issue_and_leaves_no_artifact(plan_ledger, tmp_path, monkeypatch):
    issues = Tracker()
    refuse_plans(monkeypatch, [True])
    with pytest.raises(SystemExit) as raised:
        published(plan_ledger, tmp_path, monkeypatch, issues)
    assert raised.value.code == "phase refused"
    assert issues.states() == ["CLOSED"]
    assert issues.calls[-1] == ["gh", "issue", "close", PLAN, "--reason", "not planned"]
    state = core.sync(plan_ledger)[0]
    assert (state["artifacts"], state.get(ledger_artifacts.TRASH, [])) == ([], [])
    assert [e for e in state["_meta"]["events"] if e["kind"].startswith("artifact")] == []


def test_a_retry_after_a_refused_publish_reopens_its_issue(plan_ledger, tmp_path, monkeypatch):
    issues = Tracker()
    refusing = [True]
    refuse_plans(monkeypatch, refusing)
    with pytest.raises(SystemExit):
        published(plan_ledger, tmp_path, monkeypatch, issues)
    refusing.clear()
    cli(monkeypatch, plan_ledger, "publish-plan", str(tmp_path / "plan.md"), "--phase", "p1,p2")
    assert issues.states() == ["OPEN"]
    assert verbs(issues) == ["list", "create", "close", "list", "reopen"]
    state = core.sync(plan_ledger)[0]
    assert [phase["plan_url"] for phase in state["phases"]] == [PLAN, PLAN]
    assert [(row["plan"], row["by"]) for row in state["artifacts"]] == [(True, "planner")]


def test_a_refused_republish_keeps_the_issue_its_published_plan_links(plan_ledger, tmp_path, monkeypatch):
    issues = Tracker()
    published(plan_ledger, tmp_path, monkeypatch, issues)
    refuse_plans(monkeypatch, [True])
    with pytest.raises(SystemExit):
        cli(monkeypatch, plan_ledger, "publish-plan", str(tmp_path / "plan.md"), "--phase", "p1,p2")
    assert issues.states() == ["OPEN"]
    assert verbs(issues) == ["list", "create", "list"]
    assert len(core.sync(plan_ledger)[0]["artifacts"]) == 1


def test_publish_reuses_the_open_issue_that_carries_the_stored_plan():
    issues = Tracker()
    stored = "http://127.0.0.1:8765/artifacts/demo/x.md"
    issues.rows = [
        {"url": f"{PLAN}0", "state": "CLOSED", "body": f"Old\n\n{stored}"},
        {"url": f"{PLAN}9", "state": "OPEN", "body": "Plan\n\nhttp://127.0.0.1:8765/artifacts/demo/y.md"},
        {"url": PLAN, "state": "OPEN", "body": f"Plan\n\n{stored}"},
    ]
    published = ledger_publish.publish("plan.md", "Plan", "", lambda *a: stored, issues.run, issue_title="Plan")
    assert published == (PLAN, "issue")
    assert issues.states() == ["CLOSED", "OPEN", "OPEN"]
    assert verbs(issues) == ["list"]


def test_publish_reopens_a_closed_issue_that_carries_the_stored_plan():
    issues = Tracker()
    stored = "http://127.0.0.1:8765/artifacts/demo/x.md"
    issues.rows = [{"url": PLAN, "state": "CLOSED", "body": f"Plan\n\n{stored}"}]
    published = ledger_publish.publish("plan.md", "Plan", "", lambda *a: stored, issues.run, issue_title="Plan")
    assert published == (PLAN, "issue")
    assert issues.calls[-1] == ["gh", "issue", "reopen", PLAN]
    assert issues.states() == ["OPEN"]


@pytest.mark.parametrize("verb", ["list", "reopen", "close"])
def test_an_issue_command_that_fails_names_its_verb(verb):
    issues = Tracker()
    issues.rows = [{"url": PLAN, "state": "CLOSED", "body": "Plan\n\nlink"}]

    def run(argv, **kwargs):
        if argv[2] == verb:
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 403\n")
        return issues.run(argv, **kwargs)

    with pytest.raises(ledger_publish.PublishError) as raised:
        if verb == "close":
            ledger_publish.close_issue(PLAN, run)
        else:
            ledger_publish.publish("plan.md", "Plan", "", lambda *a: "link", run, issue_title="Plan")
    assert str(raised.value) == f"gh issue {verb} failed: HTTP 403"


def test_a_refused_publish_whose_issue_will_not_close_still_names_the_refusal(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text("# Rollout\nfirst\n", encoding="utf-8")
    stub_publish(monkeypatch, [])

    def close(url):
        raise ledger_publish.PublishError(f"gh issue close failed: {url}")

    monkeypatch.setattr(ledger.ledger_publish, "close_issue", close)
    phases = {"phases": [{"id": "p1", "title": "One"}]}
    refused = {"rejected": ["x"], "_meta": {"warnings": ["phase refused"]}}
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: refused if ops else phases)
    args = ledger.build_parser().parse_args(
        ["--slug", "s", "--as", "planner", "publish-plan", str(plan), "--phase", "p1"]
    )
    with pytest.raises(SystemExit) as raised:
        ledger.cmd_publish_plan(args)
    assert raised.value.code == "phase refused"
    assert capsys.readouterr().err == f"gh issue close failed: {PLAN}\n"


def test_anchors_of_an_empty_stored_plan_are_none(monkeypatch):
    from scripts.swarm_ledger import plan_ranges

    monkeypatch.setattr(plan_ranges, "stored_text", lambda ref, doc: "")
    assert plan_ranges.anchors({}, {"plan_url": "http://127.0.0.1:8765/artifacts/s/f.md"}) == []
