import pytest

from hooks.context import conditions

pytestmark = pytest.mark.unit

FILTER = "mode: finders\nintent: Catch explanations\nfinders:\n  - script: string_literals\naction: send-back\n"


def _call(text='label = "because accounts have quota"'):
    return {"tool_name": "Write", "tool_input": {"file_path": "/repo/page.py", "content": text}, "cwd": "/missing"}


def test_builtin_named_finder_sends_a_literal_back(filters_dir):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    result = conditions.pre_effect(_call())
    assert result.block
    assert "because accounts have quota" in result.block


def test_empty_literals_do_not_hide_nonempty_findings(filters_dir):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    effect = conditions.pre_effect(_call('empty = ""\nlabel = "because accounts have quota"'))
    assert effect.block
    assert "because accounts have quota" in effect.block


def test_project_script_overrides_builtin_and_receives_contract(filters_dir, project_filters):
    directory = project_filters
    project = directory.parent.parent
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    finders = directory / "_finders"
    finders.mkdir()
    (finders / "string_literals.py").write_text(
        'import json, sys\np=json.load(sys.stdin)\nassert p["path"]=="/repo/page.py" and p["tool"]=="Write"\nprint(json.dumps([{"start":0,"end":5,"text":p["text"][:5],"reason":"project finder"}]))\n'
    )
    call = _call()
    call["cwd"] = str(project)
    result = conditions.pre_effect(call)
    assert result.block
    assert "project finder" in result.block


@pytest.mark.parametrize(
    "script",
    [
        'raise RuntimeError("broken finder")',
        'print("not json")',
        'print("{}")',
        'print(\'[ {"start": 0, "end": 500, "text": "bad", "reason": "bad"} ]\')',
    ],
)
def test_broken_script_fails_open_and_logs(filters_dir, monkeypatch, script):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    folder = filters_dir / "_finders"
    folder.mkdir()
    (folder / "string_literals.py").write_text(script)
    logs = []
    monkeypatch.setattr("hooks.filters.finders.scripts.log", lambda *args: logs.append(args))
    effect = conditions.pre_effect(_call())
    assert effect.block is None
    assert logs[0][0] == "filter finder failed"
    assert logs[0][1]["finder"] == "string_literals"


def test_shell_timeout_fails_open_without_leaving_child_pipes_open(filters_dir, monkeypatch):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    folder = filters_dir / "_finders"
    folder.mkdir()
    (folder / "string_literals.sh").write_text("sleep 1")
    monkeypatch.setattr("hooks.config.CONDITIONS_TIMEOUT_SEC", 0.1)
    logs = []
    monkeypatch.setattr("hooks.filters.finders.scripts.log", lambda *args: logs.append(args))
    from hooks.filters import runner

    result = runner.run({"path": str(filters_dir / "pre-write-slop.filter.yaml")}, "pre", _call())
    assert result["returncode"] == 0
    assert result["stdout"] == ""
    assert logs
    assert "timed out" in logs[0][1]["reason"].lower()


def test_named_explanation_tail_and_regex_compose_in_one_classifier_call(filters_dir, stub):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(
        "mode: both\nintent: Catch explanations\nfinders:\n  - script: explanation_tail\n  - regex: changed\naction: send-back\n"
    )
    fake = stub()
    effect = conditions.pre_effect(_call('label = "changed 9m ago because accounts have quota"'))
    assert effect.block
    assert "because accounts have quota" in effect.block
    assert len(fake.calls) == 1
    assert len(fake.calls[0]["questions"]) == 2


def test_shell_script_runs_and_finder_folder_is_not_a_condition(filters_dir):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    folder = filters_dir / "_finders"
    folder.mkdir()
    (folder / "string_literals.sh").write_text(
        'cat >/dev/null\nprintf \'[{"start":0,"end":5,"text":"label","reason":"shell finder"}]\''
    )
    effect = conditions.pre_effect(_call())
    assert "shell finder" in effect.block
    entries, invalid = conditions.scan_layers([("bundle", filters_dir)])
    assert len(entries) == 1
    assert invalid == []


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"start": True, "end": 5, "text": "label", "reason": "x"},
        {"start": 0, "end": 0, "text": "", "reason": "x"},
        {"start": -1, "end": 5, "text": "label", "reason": "x"},
        {"start": 0, "end": 6, "text": "wrong", "reason": "x"},
        {"start": 0, "end": 5, "text": "label", "reason": 1},
        {"start": 0.0, "end": 5, "text": "label", "reason": "x"},
    ],
)
def test_invalid_script_spans_fail_open(filters_dir, row):
    import json

    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    folder = filters_dir / "_finders"
    folder.mkdir()
    (folder / "string_literals.py").write_text(f"print({json.dumps([row])!r})")
    assert conditions.pre_effect(_call()).block is None


def test_unknown_finder_fails_open_and_logs(filters_dir, monkeypatch):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER.replace("string_literals", "missing_finder"))
    logs = []
    monkeypatch.setattr("hooks.filters.finders.scripts.log", lambda *args: logs.append(args))
    assert conditions.pre_effect(_call()).block is None
    assert logs[0][1]["finder"] == "missing_finder"


def test_untrusted_project_finder_cannot_override_package(filters_dir, project_filters, monkeypatch):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER)
    folder = project_filters / "_finders"
    folder.mkdir()
    (folder / "string_literals.py").write_text('print("[]")')
    monkeypatch.setattr(conditions, "directory_trust", lambda *args: (False, "untrusted"))
    call = _call()
    call["cwd"] = str(project_filters.parent.parent)
    assert conditions.pre_effect(call).block


def test_strip_named_findings_uses_exact_offsets(filters_dir):
    (filters_dir / "pre-write-slop.filter.yaml").write_text(FILTER.replace("send-back", "strip"))
    call = _call('label = "because accounts have quota"')
    call["permission_mode"] = "bypassPermissions"
    effect = conditions.pre_effect(call)
    assert effect.rewrite == {"file_path": "/repo/page.py", "content": 'label = ""'}


@pytest.mark.parametrize(
    "raw, message",
    [
        ({}, "finder output must be a list"),
        ([{}], "finder output must contain spans with text and reason"),
        (
            [{"start": 0, "end": 5, "text": "label", "reason": "x", "other": "extra"}],
            "finder output must contain spans with text and reason",
        ),
        ([{"start": False, "end": 5, "text": "label", "reason": "x"}], "finder output has invalid offsets"),
        ([{"start": 0, "end": 50, "text": "label", "reason": "x"}], "finder output has invalid offsets"),
        ([{"start": 0, "end": 5, "text": "wrong", "reason": "x"}], "finder output does not match the source"),
        ([{"start": 0, "end": 5, "text": "label", "reason": 1}], "finder output does not match the source"),
    ],
)
def test_invalid_finder_contract_logs_the_exact_problem(tmp_path, monkeypatch, raw, message):
    import json

    from hooks.filters.finders import scripts

    finder = tmp_path / "finder.py"
    finder.write_text(f"print({json.dumps(raw)!r})")
    logs = []
    monkeypatch.setattr(scripts, "log", lambda *args: logs.append(args))
    assert scripts.run("finder", {"finder": finder}, "label", "page.py", "Write") == []
    assert logs == [("filter finder failed", {"finder": "finder", "reason": message})]


def test_script_can_return_a_finding_through_the_last_source_character(tmp_path):
    import json

    from hooks.filters.finders import scripts

    finder = tmp_path / "finder.py"
    row = {"start": 0, "end": 5, "text": "label", "reason": "whole source"}
    finder.write_text(f"print({json.dumps([row])!r})")
    assert scripts.run("finder", {"finder": finder}, "label", "page.py", "Write") == [row]


def test_callable_finder_receives_the_tool_contract():
    from hooks.filters.finders import scripts

    def finder(text, path, tool):
        assert text == "label" and path == "page.py"
        return [{"start": 0, "end": 5, "text": text, "reason": tool}]

    assert scripts.run("finder", {"finder": finder}, "label", "page.py", "Write") == [
        {"start": 0, "end": 5, "text": "label", "reason": "Write"}
    ]


def test_finder_stderr_is_captured_and_failure_logs_exit_status(tmp_path, monkeypatch, capfd):
    from hooks.filters.finders import scripts

    finder = tmp_path / "finder.py"
    finder.write_text('import sys\nprint("finder diagnostic", file=sys.stderr)\nsys.exit(7)')
    logs = []
    monkeypatch.setattr(scripts, "log", lambda *args: logs.append(args))
    assert scripts.run("finder", {"finder": finder}, "label", "page.py", "Write") == []
    assert capfd.readouterr().err == ""
    assert logs == [("filter finder failed", {"finder": "finder", "reason": "finder exited with status 7"})]


def test_script_execution_owns_a_process_session(tmp_path):
    from hooks.filters.finders import scripts

    finder = tmp_path / "finder.py"
    finder.write_text(
        'import os,json\nassert os.getsid(0) == os.getpid()\nprint(json.dumps([{ "start":0,"end":5,"text":"label","reason":"isolated process session"}]))'
    )
    assert (
        scripts.run("finder", {"finder": finder}, "label", "page.py", "Write")[0]["reason"]
        == "isolated process session"
    )


def test_timeout_still_bounds_drain_when_process_group_kill_is_refused(tmp_path, monkeypatch):
    import os
    import signal

    from hooks.filters.finders import scripts

    finder = tmp_path / "finder.py"
    finder.write_text('import time\ntime.sleep(1.5)\nprint("[]")')
    real_killpg = os.killpg
    groups = []
    logs = []

    def refused(group, sig):
        groups.append(group)
        raise PermissionError("group kill refused")

    monkeypatch.setattr(os, "killpg", refused)
    monkeypatch.setattr(scripts.config, "CONDITIONS_TIMEOUT_SEC", 0.1)
    monkeypatch.setattr(scripts, "log", lambda *args: logs.append(args))
    try:
        assert scripts.run("finder", {"finder": finder}, "label", "page.py", "Write") == []
        assert logs[0][1]["reason"].endswith("timed out after 1 seconds")
    finally:
        for group in groups:
            try:
                real_killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_builtin_resolution_with_missing_state_and_invalid_state(monkeypatch, tmp_path):
    from hooks.context import profile_chain
    from hooks.filters.finders import scripts

    state = tmp_path / "state.json"
    monkeypatch.setattr(profile_chain, "state_path", lambda: state)
    assert scripts.run("string_literals", scripts.resolve(None), '"hello"', "page.py", "Write")[0]["text"] == "hello"
    state.write_text("[]")
    with pytest.raises(ValueError) as error:
        scripts.resolve(None)
    assert str(error.value) == "condition state must be a mapping"


def test_resolution_ignores_hidden_and_unsupported_finder_files(filters_dir):
    from hooks.filters.finders import scripts

    folder = filters_dir / "_finders"
    folder.mkdir()
    (folder / "string_literals.txt").write_text("ignored")
    (folder / "_hidden.py").write_text("ignored")
    (folder / ".hidden.sh").write_text("ignored")
    (folder / "directory.py").mkdir()
    found = scripts.resolve(None)
    assert callable(found["string_literals"])
    assert "_hidden" not in found
    assert ".hidden" not in found
    assert "directory" not in found


def test_missing_tool_name_passes_empty_tool_to_a_script(filters_dir):
    from hooks.filters import extract, runner, schema

    folder = filters_dir / "_finders"
    folder.mkdir()
    (folder / "finder.py").write_text(
        'import json,sys\np=json.load(sys.stdin)\nassert p["tool"] == ""\nprint(json.dumps([{"start":0,"end":5,"text":p["text"],"reason":"empty tool"}]))'
    )
    spec = schema.parse({"finders": [{"script": "finder"}]})
    findings = runner.find(spec, extract.pieces("Write", {"content": "label"}))
    assert findings[0].reason == "empty tool"
