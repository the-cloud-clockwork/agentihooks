import json
import subprocess
from pathlib import Path

import pytest

from scripts import ci_semgrep_floors as floors

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _done(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _finding(rule, path="hooks/a.py", line=3):
    return {"check_id": f".semgrep-guards.{rule}", "path": path, "start": {"line": line}}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    folder = tmp_path / ".semgrep-guards"
    folder.mkdir()
    monkeypatch.setattr(floors, "ROOT", tmp_path)
    monkeypatch.setattr(floors, "RULES", folder)
    monkeypatch.setattr(floors, "FLOORS", folder / "floors.json")
    return folder


def _rule(folder, name, red="# ruleid: {name}\nx()\n", clean="y()\n"):
    rule = folder / f"{name}.yml"
    rule.write_text("rules: []\n")
    if red is not None:
        (folder / f"{name}.red.py").write_text(red.format(name=name))
    if clean is not None:
        (folder / f"{name}.clean.py").write_text(clean)
    return rule


def test_every_shipped_rule_has_a_red_fixture_a_clean_file_and_a_floor():
    rules = sorted(ROOT.glob(".semgrep-guards/*.yml"))
    fixtures = sorted(path.name for path in ROOT.glob(".semgrep-guards/*.py"))
    recorded = json.loads((ROOT / ".semgrep-guards/floors.json").read_text())
    assert [rule.stem for rule in rules] == ["guard-except-returns-allow", "hook-shell-true"]
    assert fixtures == sorted(f"{rule.stem}.{kind}.py" for rule in rules for kind in ("red", "clean"))
    assert sorted(recorded) == [rule.stem for rule in rules]
    for rule in rules:
        assert f"ruleid: {rule.stem}" in rule.with_suffix(".red.py").read_text()
        assert "ruleid" not in rule.with_suffix(".clean.py").read_text()


def test_the_tree_is_the_working_directory_of_the_script():
    assert floors.RULES == floors.ROOT / ".semgrep-guards"
    assert floors.FLOORS == floors.RULES / "floors.json"
    assert floors.TARGET == "hooks"


def test_semgrep_runs_the_scan_subcommand_without_metrics_in_the_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)) or _done())
    floors._semgrep("--json", "hooks")
    assert calls == [
        (
            (["semgrep", "scan", "--metrics", "off", "--json", "hooks"],),
            {"capture_output": True, "text": True, "cwd": floors.ROOT},
        )
    ]


def test_git_runs_in_the_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)) or _done())
    floors._git("show", "x")
    assert calls == [((["git", "show", "x"],), {"capture_output": True, "text": True, "cwd": floors.ROOT})]


def test_rules_lists_only_rule_files_in_name_order(rules_dir):
    _rule(rules_dir, "b")
    _rule(rules_dir, "a")
    assert [rule.name for rule in floors.rules()] == ["a.yml", "b.yml"]


def test_findings_scan_every_rule_strictly_on_the_target(rules_dir, monkeypatch):
    calls = []
    report = {"results": [_finding("a")], "errors": []}
    monkeypatch.setattr(floors, "_semgrep", lambda *a: calls.append(a) or _done(stdout=json.dumps(report)))
    rule_files = [_rule(rules_dir, "a"), _rule(rules_dir, "b")]
    assert floors.findings(rule_files, "hooks") == report["results"]
    configs = ("--config", str(rule_files[0]), "--config", str(rule_files[1]))
    assert calls == [("--json", "--strict", "--verbose", *configs, "hooks")]


@pytest.mark.parametrize(
    ("code", "stdout"),
    [
        (2, json.dumps({"results": [], "errors": []})),
        (0, json.dumps({"results": [], "errors": [{"type": "Timeout"}]})),
        (0, json.dumps({"errors": []})),
        (0, ""),
    ],
    ids=["exit", "errors", "no-results", "no-output"],
)
def test_a_scan_that_could_not_run_is_red(rules_dir, monkeypatch, code, stdout):
    monkeypatch.setattr(floors, "_semgrep", lambda *a: _done(code, stdout, "boom"))
    with pytest.raises(SystemExit) as raised:
        floors.findings([_rule(rules_dir, "a")], "hooks")
    assert str(raised.value) == f"semgrep could not scan hooks (exit {code}):\nboom"


def test_tally_counts_each_rule_by_its_short_id_and_zero_when_absent(rules_dir):
    rule_files = [_rule(rules_dir, "a"), _rule(rules_dir, "b")]
    found = [_finding("a"), _finding("a", line=9), {"check_id": "c"}]
    assert floors.tally(rule_files, found) == {"a": 2, "b": 0}


def test_counts_tally_the_findings_of_the_target(rules_dir, monkeypatch):
    rule_files = [_rule(rules_dir, "a")]
    seen = []
    monkeypatch.setattr(floors, "findings", lambda files, target: seen.append((files, target)) or [_finding("a")])
    assert floors.counts(rule_files, "t") == {"a": 1}
    assert seen == [(rule_files, "t")]


def test_semgrep_runs_in_a_given_folder(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(k["cwd"]) or _done())
    floors._semgrep("--test", cwd=tmp_path)
    assert calls == [tmp_path]


def _report(matches=None, **extra):
    checks = {name: {"passed": True, "matches": found, "errors": []} for name, found in (matches or {}).items()}
    return {"results": {f"rules/{name}.yml": {"checks": {name: check}} for name, check in checks.items()}, **extra}


def _lines(expected, reported):
    return {"expected_lines": expected, "reported_lines": reported}


def test_fixture_test_runs_one_semgrep_test_over_a_copy_with_an_empty_ignore(rules_dir, monkeypatch):
    rule_files = [_rule(rules_dir, "a"), _rule(rules_dir, "b")]
    rules_dir.joinpath("floors.json").write_text("{}")
    seen = []

    def semgrep(*args, cwd=None):
        seen.append(
            (args, sorted(path.name for path in (cwd / "rules").iterdir()), (cwd / ".semgrepignore").read_text())
        )
        return _done(stdout=json.dumps(_report()))

    monkeypatch.setattr(floors, "_semgrep", semgrep)
    assert floors.fixture_test(rule_files) == (set(), set())
    names = ["a.clean.py", "a.red.py", "a.yml", "b.clean.py", "b.red.py", "b.yml"]
    assert seen == [(("--test", "--json", "--config", "rules", "rules"), names, "")]


def test_fixture_test_names_every_reported_and_every_mismatched_fixture(rules_dir, monkeypatch):
    report = _report(
        {
            "a": {"/t/rules/a.red.py": _lines([1], [1])},
            "b": {"/t/rules/b.red.py": _lines([1, 4], [1]), "/t/rules/b.clean.py": _lines([], [2])},
        }
    )
    monkeypatch.setattr(floors, "_semgrep", lambda *a, cwd=None: _done(1, json.dumps(report)))
    assert floors.fixture_test([_rule(rules_dir, "a"), _rule(rules_dir, "b")]) == (
        {"a.red.py", "b.red.py", "b.clean.py"},
        {"b.red.py", "b.clean.py"},
    )


@pytest.mark.parametrize(
    ("code", "stdout"),
    [
        (0, ""),
        (2, "usage: semgrep"),
        (0, json.dumps({"config_with_errors": []})),
        (0, json.dumps(_report(config_with_errors=[{"path": "rules/a.yml"}]))),
        (
            0,
            json.dumps(
                {"results": {"rules/a.yml": {"checks": {"a": {"passed": False, "matches": {}, "errors": ["x"]}}}}}
            ),
        ),
        (2, json.dumps(_report({"a": {"/t/rules/a.red.py": _lines([1], [1])}}))),
    ],
    ids=["no-output", "not-json", "no-results", "config-errors", "check-errors", "exit-without-mismatch"],
)
def test_a_fixture_test_that_could_not_run_is_red(rules_dir, monkeypatch, code, stdout):
    monkeypatch.setattr(floors, "_semgrep", lambda *a, cwd=None: _done(code, stdout, "boom"))
    with pytest.raises(SystemExit) as raised:
        floors.fixture_test([_rule(rules_dir, "a")])
    assert str(raised.value) == f"semgrep could not test the guard rule fixtures (exit {code}):\nboom"


def _fixture_errors(monkeypatch, rule_files, reported=frozenset(), mismatched=frozenset()):
    tested = []
    monkeypatch.setattr(floors, "fixture_test", lambda files: tested.append(files) or (set(reported), set(mismatched)))
    return floors.fixture_errors(rule_files), tested


@pytest.mark.parametrize("missing", ["red", "clean"])
def test_a_rule_without_both_fixtures_is_red_and_untested(rules_dir, monkeypatch, missing):
    errors, tested = _fixture_errors(monkeypatch, [_rule(rules_dir, "a", **{missing: None})])
    assert errors == ["a: needs both a.red.py and a.clean.py"]
    assert tested == []


def test_only_rules_with_both_fixtures_are_tested_in_one_pass(rules_dir, monkeypatch):
    rule_files = [_rule(rules_dir, "a"), _rule(rules_dir, "b", clean=None), _rule(rules_dir, "c")]
    errors, tested = _fixture_errors(monkeypatch, rule_files, {"a.red.py"})
    assert errors == [
        "b: needs both b.red.py and b.clean.py",
        "c: the rule does not find exactly the marked lines of c.red.py",
    ]
    assert tested == [[rule_files[0], rule_files[2]]]


def test_a_red_fixture_that_marks_no_line_is_red(rules_dir, monkeypatch):
    errors, _ = _fixture_errors(monkeypatch, [_rule(rules_dir, "a", red="# ruleid: other\nx()\n")])
    assert errors == ["a: a.red.py marks no line the rule must find"]


@pytest.mark.parametrize(
    ("reported", "mismatched"), [(set(), set()), ({"a.red.py"}, {"a.red.py"})], ids=["unreported", "mismatched"]
)
def test_a_rule_that_misses_its_marked_lines_is_red(rules_dir, monkeypatch, reported, mismatched):
    errors, _ = _fixture_errors(monkeypatch, [_rule(rules_dir, "a")], reported, mismatched)
    assert errors == ["a: the rule does not find exactly the marked lines of a.red.py"]


def test_a_rule_that_finds_something_in_its_clean_file_is_red(rules_dir, monkeypatch):
    errors, _ = _fixture_errors(monkeypatch, [_rule(rules_dir, "a")], {"a.red.py", "a.clean.py"}, {"a.clean.py"})
    assert errors == ["a: the rule finds something in a.clean.py"]


def test_a_rule_that_holds_on_both_fixtures_has_no_errors(rules_dir, monkeypatch):
    assert _fixture_errors(monkeypatch, [_rule(rules_dir, "a")], {"a.red.py"})[0] == []


def _git_at(monkeypatch, verified=0, shown=_done(stdout="text")):
    calls = []
    monkeypatch.setattr(
        floors, "_git", lambda *a: calls.append(a) or (_done(verified) if a[0] == "rev-parse" else shown)
    )
    return calls


def test_at_base_reads_the_file_at_a_verified_base_commit(rules_dir, monkeypatch):
    calls = _git_at(monkeypatch)
    assert floors.at_base("FETCH_HEAD", rules_dir / "floors.json") == "text"
    assert calls == [
        ("rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"),
        ("show", "FETCH_HEAD:.semgrep-guards/floors.json"),
    ]


def test_at_base_is_none_when_the_base_lacks_the_file(rules_dir, monkeypatch):
    _git_at(monkeypatch, shown=_done(128, "text"))
    assert floors.at_base("FETCH_HEAD", rules_dir / "floors.json") is None


def test_a_base_that_is_not_a_commit_is_red(rules_dir, monkeypatch):
    calls = _git_at(monkeypatch, verified=1)
    with pytest.raises(SystemExit) as raised:
        floors.at_base("nope", rules_dir / "floors.json")
    assert str(raised.value) == "base nope is not a commit"
    assert len(calls) == 1


def test_base_floors_parse_the_floor_file_at_the_base(rules_dir, monkeypatch):
    paths = []
    monkeypatch.setattr(floors, "at_base", lambda base, path: paths.append((base, path)) or '{"a": 2}')
    assert floors.base_floors("FETCH_HEAD") == {"a": 2}
    assert paths == [("FETCH_HEAD", rules_dir / "floors.json")]


def test_base_floors_are_empty_when_the_base_has_no_floor_file(rules_dir, monkeypatch):
    monkeypatch.setattr(floors, "at_base", lambda base, path: None)
    assert floors.base_floors("FETCH_HEAD") == {}


def test_changed_rules_are_rules_whose_text_differs_from_the_base(rules_dir, monkeypatch):
    rule_files = [_rule(rules_dir, "same"), _rule(rules_dir, "edited"), _rule(rules_dir, "new")]
    at = {"same": "rules: []\n", "edited": "rules: [old]\n", "new": None}
    bases = []
    monkeypatch.setattr(floors, "at_base", lambda base, path: bases.append(base) or at[path.stem])
    assert floors.changed_rules("FETCH_HEAD", rule_files) == {"edited"}
    assert bases == ["FETCH_HEAD"] * 3


def test_marked_cases_compare_each_red_fixture_with_the_base(rules_dir, monkeypatch):
    two = "# ruleid: {name}\nx()\n# ruleid: {name}\ny()\n"
    rule_files = [
        _rule(rules_dir, "kept", red=two),
        _rule(rules_dir, "grew", red=two),
        _rule(rules_dir, "shrank"),
        _rule(rules_dir, "new"),
        _rule(rules_dir, "gone", red=None),
    ]
    at = {
        "kept": two.format(name="kept"),
        "grew": "# ruleid: grew\n",
        "shrank": two.format(name="shrank"),
        "new": None,
        "gone": "# ruleid: gone\n",
    }
    paths = []
    monkeypatch.setattr(floors, "at_base", lambda base, path: paths.append((base, path.name)) or at[path.stem[:-4]])
    errors, grown = floors.marked_cases("FETCH_HEAD", rule_files)
    assert errors == [
        "shrank: shrank.red.py marks 1 cases, 2 at the base; fixtures only grow",
        "gone: gone.red.py marks 0 cases, 1 at the base; fixtures only grow",
    ]
    assert grown in ({"grew", "new"}, {"grew": 1, "new": 1})
    assert paths == [("FETCH_HEAD", f"{rule.stem}.red.py") for rule in rule_files]


def test_marks_count_only_the_rule_own_markers(rules_dir):
    rule = _rule(rules_dir, "a")
    assert floors._marks(rule, "# ruleid: a\n# ruleid: b\n# ok: a\n# ruleid: a\n") == 2
    assert floors._marks(rule, None) == 0


def _floor_errors(monkeypatch, rule_files, found, head, base, changed=frozenset(), grown=frozenset()):
    targets = []
    monkeypatch.setattr(floors, "findings", lambda files, target: targets.append((files, target)) or found)
    if isinstance(floors.marked_cases("FETCH_HEAD", [])[1], dict):
        grown = dict.fromkeys(grown, 1)
    errors = floors.floor_errors(head, base, rule_files, set(changed), grown)
    assert targets == [(rule_files, "hooks")]
    return errors


def test_counts_at_their_floors_have_no_errors(rules_dir, monkeypatch):
    rule_files = [_rule(rules_dir, "a"), _rule(rules_dir, "b")]
    assert _floor_errors(monkeypatch, rule_files, [_finding("a")], {"a": 1, "b": 0}, {"a": 1, "b": 0}) == []


def test_a_floor_new_since_the_base_is_accepted(rules_dir, monkeypatch):
    assert _floor_errors(monkeypatch, [_rule(rules_dir, "a")], [_finding("a")], {"a": 1}, {}) == []


def test_findings_above_the_floor_name_every_site(rules_dir, monkeypatch):
    found = [_finding("a", "hooks/x.py", 4), _finding("b"), _finding("a", "hooks/y.py", 7)]
    rule_files = [_rule(rules_dir, "a"), _rule(rules_dir, "b")]
    errors = _floor_errors(monkeypatch, rule_files, found, {"a": 1, "b": 1}, {"a": 1, "b": 1})
    assert errors == ["a: 2 findings above the floor of 1:\n  hooks/x.py:4\n  hooks/y.py:7"]


def test_findings_under_the_floor_ask_for_a_new_record(rules_dir, monkeypatch):
    errors = _floor_errors(monkeypatch, [_rule(rules_dir, "a")], [], {"a": 1}, {"a": 1})
    assert errors == ["a: 0 findings under the floor of 1; run python -m scripts.ci_semgrep_floors record"]


def test_a_floor_lowered_with_an_unchanged_rule_is_accepted(rules_dir, monkeypatch):
    assert _floor_errors(monkeypatch, [_rule(rules_dir, "a")], [_finding("a")], {"a": 1}, {"a": 2}) == []


def test_a_floor_raised_against_the_base_is_red(rules_dir, monkeypatch):
    found = [_finding("a"), _finding("a")]
    errors = _floor_errors(monkeypatch, [_rule(rules_dir, "a")], found, {"a": 2}, {"a": 1})
    assert errors == [RISE]


RISE = "a: floor rose from 1 to 2; a floor rises only with a rule change that adds red fixture cases"


@pytest.mark.parametrize(("changed", "grown"), [({"a"}, set()), (set(), {"a"})], ids=["rule-only", "fixture-only"])
def test_a_floor_rise_needs_both_a_rule_change_and_new_red_cases(rules_dir, monkeypatch, changed, grown):
    found = [_finding("a"), _finding("a")]
    errors = _floor_errors(monkeypatch, [_rule(rules_dir, "a")], found, {"a": 2}, {"a": 1}, changed, grown)
    assert errors == [RISE]


def test_a_rule_change_with_new_red_cases_may_raise_its_own_floor(rules_dir, monkeypatch):
    found = [_finding("a"), _finding("a")]
    assert _floor_errors(monkeypatch, [_rule(rules_dir, "a")], found, {"a": 2}, {"a": 1}, {"a"}, {"a"}) == []


@pytest.mark.parametrize(("added", "rise"), [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3)])
def test_floor_rise_is_bounded_by_added_red_cases(rules_dir, monkeypatch, added, rise):
    rule = _rule(rules_dir, "a", red="# ruleid: {name}\nx()\n" * (1 + added))
    monkeypatch.setattr(floors, "at_base", lambda base, path: "# ruleid: a\nx()\n")
    errors, grown = floors.marked_cases("FETCH_HEAD", [rule])
    assert errors == []
    monkeypatch.setattr(floors, "findings", lambda files, target: [_finding("a")] * (1 + rise))
    errors = floors.floor_errors({"a": 1 + rise}, {"a": 1}, [rule], {"a"}, grown)
    assert grown in ({"a"}, {"a": added})
    if isinstance(grown, dict) and rise > added:
        assert errors == [f"a: floor rose by {rise} but added only {added} red fixture cases"]
    else:
        assert errors == []


def test_a_floor_dropped_with_a_rule_change_is_red(rules_dir, monkeypatch):
    errors = _floor_errors(monkeypatch, [_rule(rules_dir, "a")], [_finding("a")], {"a": 1}, {"a": 2}, {"a"})
    assert errors == [
        "a: floor dropped from 2 to 1 with a rule change; change the rule and lower its floor in separate pull requests"
    ]


def test_a_rule_change_that_keeps_its_floor_is_accepted(rules_dir, monkeypatch):
    found = [_finding("a"), _finding("a")]
    assert _floor_errors(monkeypatch, [_rule(rules_dir, "a")], found, {"a": 2}, {"a": 2}, {"a"}) == []


def test_a_rule_without_a_floor_is_red(rules_dir, monkeypatch):
    errors = _floor_errors(monkeypatch, [_rule(rules_dir, "a")], [], {}, {})
    assert errors == ["a: no floor; run python -m scripts.ci_semgrep_floors record"]


def test_a_rule_without_a_floor_still_grades_the_rules_after_it(rules_dir, monkeypatch):
    rule_files = [_rule(rules_dir, "a"), _rule(rules_dir, "b")]
    errors = _floor_errors(monkeypatch, rule_files, [_finding("b")], {"b": 0}, {"b": 0})
    assert errors == [
        "a: no floor; run python -m scripts.ci_semgrep_floors record",
        "b: 1 findings above the floor of 0:\n  hooks/a.py:3",
    ]


def test_a_floor_without_a_rule_file_is_red(rules_dir, monkeypatch):
    errors = _floor_errors(monkeypatch, [_rule(rules_dir, "a")], [], {"a": 0, "gone": 1, "old": 0}, {})
    assert errors == ["gone: floor without a rule file", "old: floor without a rule file"]


def test_a_rule_removed_since_the_base_is_red(rules_dir, monkeypatch):
    errors = _floor_errors(monkeypatch, [_rule(rules_dir, "a")], [], {"a": 0}, {"a": 0, "gone": 0, "old": 1})
    assert errors == [
        "gone: guard rule removed since the base; floors only shrink",
        "old: guard rule removed since the base; floors only shrink",
    ]


def test_check_without_rule_files_is_red(rules_dir, capfd):
    assert floors.check("FETCH_HEAD") == 1
    assert capfd.readouterr().out == "no rule files in .semgrep-guards\n"


def _check_mocks(
    monkeypatch, fixture=lambda files: [], errors=lambda head, base, files, changed, grown: [], marked=([], {"b"})
):
    seen = []
    monkeypatch.setattr(floors, "fixture_errors", fixture)
    monkeypatch.setattr(floors, "marked_cases", lambda base, files: seen.append(("marked", base, len(files))) or marked)
    monkeypatch.setattr(floors, "base_floors", lambda base: seen.append(("floors", base)) or {"a": 4})
    monkeypatch.setattr(
        floors, "changed_rules", lambda base, files: seen.append(("changed", base, len(files))) or {"a"}
    )
    monkeypatch.setattr(floors, "floor_errors", errors)
    return seen


def test_check_prints_every_fixture_and_floor_error(rules_dir, monkeypatch, capfd):
    _rule(rules_dir, "a")
    _rule(rules_dir, "b")
    rules_dir.joinpath("floors.json").write_text('{"a": 3}')
    seen = _check_mocks(
        monkeypatch,
        fixture=lambda files: [f"fixture {rule.stem}" for rule in files],
        errors=lambda head, base, files, changed, grown: [
            f"floor {head} {base} {len(files)} {sorted(changed)} {sorted(grown)}"
        ],
        marked=(["shrunk"], {"b"}),
    )
    assert floors.check("FETCH_HEAD") == 1
    assert seen == [("marked", "FETCH_HEAD", 2), ("floors", "FETCH_HEAD"), ("changed", "FETCH_HEAD", 2)]
    assert capfd.readouterr().out == "fixture a\nfixture b\nshrunk\nfloor {'a': 3} {'a': 4} 2 ['a'] ['b']\n"


def test_check_without_a_floor_file_grades_against_no_floors(rules_dir, monkeypatch):
    heads = []
    _rule(rules_dir, "a")
    _check_mocks(monkeypatch, errors=lambda head, base, files, changed, grown: heads.append(head) or ["x"])
    assert floors.check("FETCH_HEAD") == 1
    assert heads == [{}]


def test_check_reports_the_floors_when_every_rule_holds(rules_dir, monkeypatch, capfd):
    _rule(rules_dir, "a")
    rules_dir.joinpath("floors.json").write_text('{"a": 2}')
    _check_mocks(monkeypatch)
    assert floors.check("FETCH_HEAD") == 0
    assert capfd.readouterr().out == '1 guard rules hold on their fixtures and at their floors: {"a": 2}\n'


def test_record_writes_the_measured_counts_of_the_hooks(rules_dir, monkeypatch, capfd):
    rule_files = [_rule(rules_dir, "b"), _rule(rules_dir, "a")]
    seen = []
    monkeypatch.setattr(floors, "counts", lambda files, target: seen.append((files, target)) or {"b": 0, "a": 2})
    assert floors.record() == 0
    assert seen == [(sorted(rule_files), "hooks")]
    assert rules_dir.joinpath("floors.json").read_text() == '{\n  "a": 2,\n  "b": 0\n}\n'
    assert capfd.readouterr().out == '{"b": 0, "a": 2}\n'


def test_main_runs_check_with_its_base(monkeypatch):
    monkeypatch.setattr(floors, "check", lambda base: 7 if base == "FETCH_HEAD" else 0)
    assert floors.main(["check", "--base", "FETCH_HEAD"]) == 7


def test_main_runs_record(monkeypatch):
    monkeypatch.setattr(floors, "record", lambda: 5)
    assert floors.main(["record"]) == 5


@pytest.mark.parametrize("argv", [[], ["check"], ["other"]], ids=["no-command", "no-base", "unknown"])
def test_main_refuses_an_incomplete_command(argv):
    with pytest.raises(SystemExit) as raised:
        floors.main(argv)
    assert raised.value.code == 2


def test_main_describes_the_gate_in_its_help(capfd):
    with pytest.raises(SystemExit):
        floors.main(["--help"])
    lines = capfd.readouterr().out.splitlines()
    assert "Grade the guard Semgrep rules against their fixtures and floors." in lines
