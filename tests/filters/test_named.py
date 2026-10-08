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
    (folder / "string_literals.sh").write_text("sleep 30")
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
