import argparse
import json
import re
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import (
    ledger,
    ledger_artifacts,
    ledger_phases,
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
        return subprocess.CompletedProcess(argv, 0, f"Creating issue in acme/app\n\n{created}\n", "")

    return run, calls


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


UNMARKED = (
    "plan task sections need a slice marker on the line before each heading, written as "
    "<!-- slice: name --> on its own line: {}"
)


@pytest.mark.parametrize(
    ("text", "phases", "missing"),
    [
        ("# Plan\n## Build\n<!-- slice: first -->\n### First\nOne\n### Second\nTwo\n", "p1", "Second"),
        ("# Plan\n## Build\n### First\n## Ship\n<!-- slice: s -->\n### Pack\n### Send\n", "p1,p2", "First, Send"),
        ("intro\n# Plan\n## First\n\n<!-- slice: two -->\n## Two\n", "p1", "First"),
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


def test_publish_plan_takes_a_fully_marked_plan(plan_ledger, tmp_path, monkeypatch, capsys):
    text = (
        "# Plan\n## Build\nIntro\n<!-- slice: first -->\n\n### First\n#### Detail\n"
        "```\n### Not a heading\n```\n<!-- slice: second -->\n### Second\n"
    )
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
    assert phase["plan_ref"] == {"artifact": url, "lines": "2-12"}
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
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: {"id": f"{'d' * 64}.md"})
    monkeypatch.setattr(ledger, "send", lambda *a, **k: None)


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
    assert titles == ["Plan for phases p1, p2"]


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
