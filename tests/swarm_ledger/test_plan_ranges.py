import json
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_artifacts, ledger_publish
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_kind
from tests.swarm_ledger.test_plan_kind import add, update
from tests.swarm_ledger.test_plan_publish import cli, task

plan_ledger = test_plan_kind.plan_ledger

PLAN = "# Plan\n\n## Build\n<!-- slice: first -->\n### First\nOne\n<!-- slice: second -->\n### Second\nTwo\n<!-- slice: third -->\n### Third\nThree\n## Ship\nOther\n"


@pytest.fixture
def published(plan_ledger, tmp_path, monkeypatch, capsys):
    core.sync(plan_ledger, ops=[{"op": "join", "id": "join", "by": "planner", "role": "member"}])
    path = tmp_path / "plan.md"
    path.write_text(PLAN)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.setattr(ledger.ledger_publish, "has_issues", lambda repo, run=None: False)
    monkeypatch.setattr(
        ledger,
        "upload_artifact",
        lambda slug, name, path, request: ledger_artifacts.store(slug, "plan.md", PLAN.encode()),
    )
    cli(monkeypatch, plan_ledger, "publish-plan", str(path), "--phase", "p1")
    capsys.readouterr()
    return plan_ledger


def test_issue_publication_always_stores_artifact():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps({"hasIssuesEnabled": True})
            if argv[1] == "repo"
            else "https://github.com/acme/app/issues/1\n",
        )

    saved = []
    ledger_publish.publish(
        "plan.md",
        "Build",
        "acme/app",
        lambda *args: saved.append(args) or "http://localhost/artifacts/demo/file.md",
        run,
    )
    assert saved == [("plan.md", "Build")]
    assert calls[1] == [
        "gh",
        "issue",
        "create",
        "--title",
        "Build",
        "--body",
        "Build\n\nhttp://localhost/artifacts/demo/file.md",
        "--repo",
        "acme/app",
    ]


def test_publication_and_three_computed_ranges(published, monkeypatch):
    state = core.sync(published)[0]
    [artifact] = state["artifacts"]
    assert artifact["plan"] is True
    ref = state["phases"][0]["plan_ref"]
    assert ref == {"artifact": f"{ledger.BASE}/artifacts/{published}/{artifact['file']['id']}", "lines": "3-12"}
    for name, expected in [("first", "4-6"), ("second", "7-9"), ("third", "10-12")]:
        cli(monkeypatch, published, "task", "add", name, name.title(), "--phase", "p1", "--plan-slice", name)
        state = core.sync(published)[0]
        assert task(state, name)["plan_lines"] == expected
        assert task(state, name)["plan_slice"] == name
    add(published, "plan", lane="plan", kind="plan")
    state, rejected = update(published, state="done", proof={"slice": "first,second,third"})
    assert rejected == []
    assert task(state, "plan")["state"] == "done"


def test_missing_anchor_refused(published):
    state, rejected = add(published, "missing", plan_slice="missing")
    assert rejected == ["add-missing"]
    assert "slice anchor missing is missing" in state["_meta"]["warnings"][0]
    assert not any(t["id"] == "missing" for t in state["tasks"])


def test_slice_without_range_refused(plan_ledger):
    add(plan_ledger, "plan", lane="plan", kind="plan")
    add(plan_ledger, "first", plan_url="https://github.com/acme/app/issues/1")
    state, rejected = update(plan_ledger, state="done", proof={"slice": "first"})
    assert rejected == ["finish-plan"]
    assert task(state, "plan")["state"] == "open"


def lines_of(text, name, phase_range=None):
    from scripts.swarm_ledger import plan_ranges

    return plan_ranges.slice_lines(text, name, phase_range or f"1-{len(text.splitlines())}")


def test_anchor_after_its_heading_starts_at_the_heading():
    text = "## Build\n### First\n<!-- slice: first -->\nOne\n\n### Second\n<!-- slice: second -->\nTwo\n"
    assert lines_of(text, "first") == "2-4"
    assert lines_of(text, "second") == "6-8"


def test_plain_text_sections_run_to_the_next_anchor_or_phase_heading():
    text = "## Build\n<!-- slice: a -->\nAlpha\n#### Detail\nmore\n<!-- slice: b -->\nBeta\n\n## Ship\n"
    assert lines_of(text, "a") == "2-5"
    assert lines_of(text, "b") == "6-7"


def test_anchor_ranges_stay_inside_their_phase_and_skip_fenced_text():
    text = "## Build\n<!-- slice: a -->\n```\n## not a heading\n<!-- slice: b -->\n```\n## Ship\n<!-- slice: b -->\nB\n"
    assert lines_of(text, "a", "1-6") == "2-6"
    with pytest.raises(ValueError, match="slice anchor b is missing or repeated in its phase"):
        lines_of(text, "b", "1-6")
    assert lines_of(text, "b", "7-9") == "8-9"


def test_repeated_anchor_is_refused():
    with pytest.raises(ValueError, match="missing or repeated"):
        lines_of("<!-- slice: a -->\nx\n<!-- slice: a -->\n", "a")


@pytest.mark.parametrize("bad", ["", "0-3", "4-2", "a-b", "3", 7, None])
def test_plan_lines_must_be_an_ordered_range(bad):
    from scripts.swarm_ledger import plan_ranges

    with pytest.raises(ValueError, match="plan lines must be an ordered inclusive range"):
        plan_ranges.bounds(bad)


@pytest.mark.parametrize(
    "ref",
    [
        None,
        {"artifact": "http://h/artifacts/s/x.md"},
        {"artifact": "plan", "lines": "1-2"},
        {"artifact": "http://h/a", "lines": "1-2", "x": 1},
    ],
)
def test_plan_ref_is_checked_as_a_phase_field(ref):
    from scripts.swarm_ledger import ledger_phases

    with pytest.raises(ValueError):
        ledger_phases.check_fields({"plan_ref": ref})


def test_slice_needs_a_published_plan_artifact(plan_ledger):
    state, rejected = add(plan_ledger, "early", plan_slice="early")
    assert rejected == ["add-early"]
    assert state["_meta"]["warnings"][0] == "publish a plan artifact for the phase before adding a plan slice"
