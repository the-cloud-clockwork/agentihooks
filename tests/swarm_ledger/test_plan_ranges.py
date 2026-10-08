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
        issue_title="Build",
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


def test_anchor_after_its_heading_runs_to_the_next_heading_of_that_level():
    text = "## Build\n### First\n<!-- slice: first -->\nOne\n\n### Second\n<!-- slice: second -->\nTwo\n"
    assert lines_of(text, "first") == "3-4"
    assert lines_of(text, "second") == "7-8"


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


def test_a_fence_closes_only_on_a_bare_marker_and_heading_titles_keep_inner_hashes():
    from scripts.swarm_ledger import plan_ranges

    text = "# C#\n````\n```python\n## Inside\n```\n````\n# Ship ##\n"
    assert [(n, level, title) for n, level, title in plan_ranges.sections(text) if level] == [
        (1, 1, "C#"),
        (7, 1, "Ship"),
    ]
    assert plan_ranges.phase_lines(text, [{"id": "p1", "title": "C#"}, {"id": "p2", "title": "Ship"}]) == {
        "p1": "1-6",
        "p2": "7-7",
    }


def test_a_range_past_the_plan_end_is_clamped():
    assert lines_of("## Build\n<!-- slice: a -->\nmore\n", "a", "1-99") == "2-3"


def test_a_shallower_heading_inside_the_phase_ends_a_deeper_task_section():
    text = "# Build\n## Parser\n### First\n<!-- slice: first -->\nOne\n## Pages\nOther\n# Ship\n"
    phase_range = "1-7"
    assert lines_of(text, "first", phase_range) == "4-5"


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


def test_typed_phase_lines_are_refused(published):
    ref = core.sync(published)[0]["phases"][0]["plan_ref"]
    op = {"op": "phase_update", "id": "typed", "by": "planner", "item": "phases/p1"}
    op["fields"] = {"plan_ref": {**ref, "lines": "1-5"}}
    core.check_op(op)
    state, rejected = core.sync(published, ops=[op])
    assert rejected == ["typed"]
    assert state["_meta"]["warnings"][0] == "plan_ref lines for phase p1 must be the range computed from its plan"
    assert state["phases"][0]["plan_ref"] == ref


def refusal(call, *args):
    with pytest.raises(ValueError) as raised:
        call(*args)
    return str(raised.value)


def test_plan_range_checks_name_each_rule():
    from scripts.swarm_ledger import plan_ranges

    assert plan_ranges.bounds("3-3") == (3, 3)
    assert refusal(plan_ranges.bounds, "4-3") == "plan lines must be an ordered inclusive range"
    good = {"artifact": "https://h/artifacts/s/x.md", "lines": "1-2"}
    plan_ranges.check_ref(good)
    plan_ranges.check_ref({**good, "artifact": "http://h/artifacts/s/x.md"})
    assert refusal(plan_ranges.check_ref, {"artifact": good["artifact"]}) == "plan_ref needs artifact and lines"
    for link in (5, "ftp://h/x", "http:///x"):
        assert (
            refusal(plan_ranges.check_ref, {**good, "artifact": link}) == "plan artifact must be an http or https link"
        )


def test_phase_ranges_without_headings():
    from scripts.swarm_ledger import plan_ranges

    one, two = {"id": "p1", "title": "Build"}, {"id": "p2", "title": "Ship"}
    assert plan_ranges.phase_lines("intro\nmore\n", [one]) == {"p1": "1-2"}
    assert refusal(plan_ranges.phase_lines, "", [one]) == "plan needs one heading for phase Build"
    assert refusal(plan_ranges.phase_lines, "# Build\n", [one, two]) == "plan needs one heading for phase Ship"
    assert refusal(plan_ranges.phase_lines, "# Build\n# Build\n", [one]) == "plan needs one heading for phase Build"


def test_an_indented_fence_hides_its_headings():
    from scripts.swarm_ledger import plan_ranges

    text = "  ```\n## Inside\n  ```\n## Out\n"
    assert [(n, level) for n, level, _ in plan_ranges.sections(text) if level] == [(4, 2)]


def test_headings_and_fences_follow_markdown():
    from scripts.swarm_ledger import plan_ranges

    lines = ["####### seven", "#tight", "##", "## Ship ##", "# a #b", "# #", "  ### Indented", "###### Six", "# Box X"]
    text = "\n".join([*lines, "``", "## After two ticks", "~~~ text", "## Hidden", "~~~", "## Shown"]) + "\n"
    assert [(n, level, title) for n, level, title in plan_ranges.sections(text) if level] == [
        (4, 2, "Ship"),
        (5, 1, "a #b"),
        (6, 1, ""),
        (8, 6, "Six"),
        (9, 1, "Box X"),
        (11, 2, "After two ticks"),
        (15, 2, "Shown"),
    ]


def test_a_level_one_heading_ends_a_slice_and_a_deeper_heading_does_not_set_its_level():
    assert lines_of("<!-- slice: a -->\nx\n# Next\ny\n", "a") == "1-2"
    assert lines_of("## Build\n<!-- slice: a -->\nAlpha\n### Detail\nmore\n", "a") == "2-5"


def test_stored_text_names_each_refusal(published):
    from scripts.swarm_ledger import plan_ranges

    state = core.sync(published)[0]
    ref = state["phases"][0]["plan_ref"]
    assert plan_ranges.stored_text(ref, state) == PLAN
    base, file_id = ref["artifact"].rsplit("/", 1)
    for link in (f"{base}/{file_id}/extra", ref["artifact"].replace("/artifacts/", "/media/")):
        message = refusal(plan_ranges.stored_text, {**ref, "artifact": link}, state)
        assert message == "plan artifact must name a stored ledger artifact"
    unmarked = {"artifacts": [{**state["artifacts"][0], "plan": False}]}
    for doc in ({}, unmarked):
        assert refusal(plan_ranges.stored_text, ref, doc) == "plan artifact is missing or is not marked as a plan"


def test_slice_tasks_of_a_missing_phase_or_without_a_slice_are_invalid():
    from scripts.swarm_ledger import plan_ranges

    doc = {"phases": [], "artifacts": [], "tasks": [{"id": "a", "plan_slice": "a"}, {"id": "b"}, {"id": "c"}]}
    assert plan_ranges.invalid_tasks({"phase": "gone"}, doc, ["a", "b"]) == ["a", "b"]
