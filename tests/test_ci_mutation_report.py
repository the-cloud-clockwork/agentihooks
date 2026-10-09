from pathlib import Path

import pytest

from scripts.ci_mutation.report import evaluate, mutation_lines, parse_results


def test_report_counts_every_survivor_and_only_changed_lines_fail():
    results = parse_results("\n".join(f"    hooks.sample.x_f__mutmut_{n}: survived" for n in range(150)))
    assert len(results) == 150
    rows = [{"name": name, "status": status, "lines": [10], "fingerprint": "abc"} for name, status in results]
    report = evaluate("hooks/sample.py", rows, {11}, {})
    assert report["counts"] == {"survived": 150}
    assert len(report["untouched_survivors"]) == 150
    assert report["failures"] == []
    report = evaluate("hooks/sample.py", rows, {10}, {})
    assert len(report["failures"]) == 150


def test_mutant_mapping_excludes_context_and_uses_original_function_offset():
    original = "@decorator\ndef f():\n    before = 1\n    return 3\n"
    mutated = "@decorator\ndef f():\n    before = 1\n    return 4\n"
    assert mutation_lines(original, mutated, 20) == {23}


def test_clearance_matches_exact_mutation_and_requires_reader_and_reason():
    row = {"name": "hooks.sample.x_f__mutmut_1", "status": "survived", "lines": [10], "fingerprint": "abc"}
    key = "hooks/sample.py:hooks.sample.x_f__mutmut_1:abc"
    cleared = {key: {"reader": "Standards", "reason": "The message has no observable effect"}}
    assert evaluate("hooks/sample.py", [row], {10}, cleared)["failures"] == []
    assert len(evaluate("hooks/sample.py", [row], {10}, {})["failures"]) == 1
    with pytest.raises(ValueError, match="reader and reason"):
        evaluate("hooks/sample.py", [row], {10}, {key: {"reason": ""}})


@pytest.mark.parametrize(
    "status", ["not checked", "timeout", "check was interrupted by user", "suspicious", "segfault"]
)
def test_incomplete_results_fail_even_on_untouched_lines(status):
    row = {"name": "hooks.sample.x_f__mutmut_1", "status": status, "lines": [10], "fingerprint": "abc"}
    assert len(evaluate("hooks/sample.py", [row], {11}, {})["failures"]) == 1


def test_uncovered_mutant_is_a_survivor_and_unrecognised_report_fails():
    row = {"name": "hooks.sample.x_f__mutmut_1", "status": "no tests", "lines": [10], "fingerprint": "abc"}
    assert len(evaluate("hooks/sample.py", [row], {10}, {})["failures"]) == 1
    with pytest.raises(ValueError, match="result"):
        parse_results("bad output")


def test_function_offsets_cover_decorated_methods_and_async_functions():
    from scripts.ci_mutation.report import function_start

    source = "\nclass Example:\n    @decorator\n    def f(self):\n        return 1\n\nasync def run():\n    return 2\n"
    assert function_start(source, "@decorator\ndef f(self):\n    return 1", "Example") == 3
    assert function_start(source, "async def run():\n    return 2", None) == 7


def test_report_reads_real_mutmut_metadata_and_maps_original_lines(tmp_path, monkeypatch):
    import json

    from scripts.ci_mutation.report import collect_results

    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "mutants" / "hooks").mkdir(parents=True)
    source = "\n\ndef value():\n    return 7\n"
    (tmp_path / "hooks/sample.py").write_text(source)
    (tmp_path / "setup.cfg").write_text("[mutmut]\nsource_paths=hooks/\n")
    (tmp_path / "mutants/hooks/sample.py").write_text(
        "def x_value__mutmut_orig():\n    return 7\n\ndef x_value__mutmut_1():\n    return 8\n\ndef x_value__mutmut_2():\n    return None\n"
    )
    meta = {
        "exit_code_by_key": {"hooks.sample.x_value__mutmut_1": 0, "hooks.sample.x_value__mutmut_2": 1},
        "durations_by_key": {},
        "estimated_durations_by_key": {},
    }
    (tmp_path / "mutants/hooks/sample.py.meta").write_text(json.dumps(meta))
    (tmp_path / "hooks/sample_extra.py").write_text("def value():\n    return 7\n")
    (tmp_path / "mutants/hooks/sample_extra.py").write_text("def x_value__mutmut_orig():\n    return 7\n")
    extra = {**meta, "exit_code_by_key": {"hooks.sample_extra.x_value__mutmut_1": 0}}
    (tmp_path / "mutants/hooks/sample_extra.py.meta").write_text(json.dumps(extra))
    monkeypatch.chdir(tmp_path)
    rows = collect_results(Path("hooks/sample.py"))
    assert len(rows) == 2
    assert rows[0]["status"] == "survived"
    assert rows[0]["lines"] == [4]
    assert rows[0]["name"] == "hooks.sample.x_value__mutmut_1"
    assert rows[0]["fingerprint"] == "80996a57170fdd691ed692293a179b6df12c650a3696e9a30bb2da4e67353263"
    assert rows[1] == {"name": "hooks.sample.x_value__mutmut_2", "status": "killed", "lines": [], "fingerprint": ""}


def test_parse_results_keeps_entries_after_blank_lines():
    assert parse_results("\n hooks.sample.x_f__mutmut_1: survived\n\n hooks.sample.x_f__mutmut_2: killed\n") == [
        ("hooks.sample.x_f__mutmut_1", "survived"),
        ("hooks.sample.x_f__mutmut_2", "killed"),
    ]


def test_full_report_keeps_every_row_and_clearance_details():
    killed = {"name": "killed", "status": "killed", "lines": [], "fingerprint": ""}
    pending = {"name": "pending", "status": "not checked", "lines": [3], "fingerprint": "p"}
    uncovered = {"name": "uncovered", "status": "no tests", "lines": [3], "fingerprint": "u"}
    survivor = {"name": "survivor", "status": "survived", "lines": [2], "fingerprint": "s"}
    rows = [killed, pending, uncovered, survivor]
    clearance = {"reader": "Standards", "reason": "No output changes"}
    assert evaluate("hooks/sample.py", rows, {2}, {"hooks/sample.py:survivor:s": clearance}) == {
        "path": "hooks/sample.py",
        "counts": {"killed": 1, "not checked": 1, "no tests": 1, "survived": 1},
        "failures": [pending],
        "untouched_survivors": [uncovered],
        "cleared": [{**survivor, **clearance}],
    }
    assert evaluate("hooks/sample.py", rows, {2}, {})["failures"] == [pending, survivor]


@pytest.mark.parametrize("entry", [{"reader": "", "reason": "x"}, {"reader": "Standards", "reason": ""}])
def test_invalid_clearances_have_an_exact_error(entry):
    row = {"name": "m", "status": "survived", "lines": [2], "fingerprint": "s"}
    with pytest.raises(ValueError) as error:
        evaluate("hooks/sample.py", [row], {2}, {"hooks/sample.py:m:s": entry})
    assert str(error.value) == "Mutation clearance requires reader and reason"


def test_mapping_handles_insertions_and_repeated_lines():
    assert mutation_lines("def f():\n    return 1", "def f():\n    value = 1\n    return 1", 10) == {11}
    before = ["def f():", *["    same = 1"] * 210, "    return 2"]
    after = ["def f():", "    same = 2", *["    same = 1"] * 210, "    return 3"]
    assert mutation_lines("\n".join(before), "\n".join(after), 1) == {2, 212}


def test_function_offset_selects_named_class_and_function():
    from scripts.ci_mutation.report import function_start

    source = "class Other:\n    def f(self):\n        return 1\n\nclass Example:\n    def other(self):\n        return 1\n    def f(self):\n        return 2\n"
    assert function_start(source, "def f(self):\n    return 2", "Example") == 8


def test_report_maps_a_method_mutant_back_to_its_class(tmp_path, monkeypatch):
    import json

    from scripts.ci_mutation.report import collect_results

    for name in ("hooks", "scripts", "mutants/hooks"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "hooks/sample.py").write_text("class Example:\n    def value(self):\n        return 7\n")
    (tmp_path / "setup.cfg").write_text("[mutmut]\nsource_paths=hooks/\n")
    (tmp_path / "mutants/hooks/sample.py").write_text(
        "class Example:\n    def xǁExampleǁvalue__mutmut_orig(self):\n        return 7\n    def xǁExampleǁvalue__mutmut_1(self):\n        return 8\n"
    )
    meta = {
        "exit_code_by_key": {"hooks.sample.xǁExampleǁvalue__mutmut_1": 0},
        "durations_by_key": {},
        "estimated_durations_by_key": {},
    }
    (tmp_path / "mutants/hooks/sample.py.meta").write_text(json.dumps(meta))
    monkeypatch.chdir(tmp_path)
    assert collect_results(Path("hooks/sample.py"))[0]["lines"] == [3]


def test_report_reads_only_the_mutants_its_shard_ran(tmp_path, monkeypatch):
    import json

    from scripts.ci_mutation.report import collect_results

    for name in ("hooks", "scripts", "mutants/hooks"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "hooks/sample.py").write_text("def value(a):\n    return a + 7\n")
    (tmp_path / "setup.cfg").write_text("[mutmut]\nsource_paths=hooks/\n")
    (tmp_path / "mutants/hooks/sample.py").write_text(
        "def x_value__mutmut_orig(a):\n    return a + 7\n"
        "def x_value__mutmut_1(a):\n    return a - 7\n"
        "def x_value__mutmut_2(a):\n    return a + 8\n"
    )
    meta = {
        "exit_code_by_key": {"hooks.sample.x_value__mutmut_1": 1, "hooks.sample.x_value__mutmut_2": None},
        "durations_by_key": {},
        "estimated_durations_by_key": {},
    }
    (tmp_path / "mutants/hooks/sample.py.meta").write_text(json.dumps(meta))
    monkeypatch.chdir(tmp_path)
    assert [row["name"] for row in collect_results(Path("hooks/sample.py"))] == [
        "hooks.sample.x_value__mutmut_1",
        "hooks.sample.x_value__mutmut_2",
    ]
    assert collect_results(Path("hooks/sample.py"), (0, 2)) == [
        {"name": "hooks.sample.x_value__mutmut_1", "status": "killed", "lines": [], "fingerprint": ""}
    ]
    assert [row["status"] for row in collect_results(Path("hooks/sample.py"), (1, 2))] == ["not checked"]


@pytest.mark.parametrize("method", [False, True])
def test_report_maps_overloaded_implementations_instead_of_stubs(tmp_path, monkeypatch, method):
    import json

    from scripts.ci_mutation.report import collect_results

    for name in ("hooks", "scripts", "mutants/hooks"):
        (tmp_path / name).mkdir(parents=True)
    if method:
        source = "from typing import overload\nclass Example:\n    @overload\n    def value(self) -> int: ...\n    def value(self):\n        return 7\n"
        generated = "class Example:\n    def xǁExampleǁvalue__mutmut_orig(self):\n        return 7\n    def xǁExampleǁvalue__mutmut_1(self):\n        return 8\n"
        key = "hooks.sample.xǁExampleǁvalue__mutmut_1"
    else:
        source = "from typing import overload\n@overload\ndef value() -> int: ...\n\ndef value():\n    return 7\n"
        generated = "def x_value__mutmut_orig():\n    return 7\n\ndef x_value__mutmut_1():\n    return 8\n"
        key = "hooks.sample.x_value__mutmut_1"
    (tmp_path / "hooks/sample.py").write_text(source)
    (tmp_path / "setup.cfg").write_text("[mutmut]\nsource_paths=hooks/\n")
    (tmp_path / "mutants/hooks/sample.py").write_text(generated)
    meta = {"exit_code_by_key": {key: 0}, "durations_by_key": {}, "estimated_durations_by_key": {}}
    (tmp_path / "mutants/hooks/sample.py.meta").write_text(json.dumps(meta))
    monkeypatch.chdir(tmp_path)
    rows = collect_results(Path("hooks/sample.py"))
    assert rows[0]["lines"] == [6]
    assert evaluate("hooks/sample.py", rows, {6}, {})["failures"] == rows
