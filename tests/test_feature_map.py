import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import feature_map


@pytest.fixture
def mapped_repo(tmp_path):
    rules = tmp_path / ".semgrep/features"
    rules.mkdir(parents=True)
    (rules / "login.yml").write_text("rules:\n  - id: feature-login\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_login.py").write_text(
        'import pytest\n@pytest.mark.feature("login")\ndef test_shared():\n    assert True\n'
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "live.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    entry = {
        "id": "login",
        "origin": "operator",
        "seam": "The front door launch command",
        "operator_words": "All front doors use the same stored login.",
        "ordered_at": "2026-10-25",
        "ledger_item": "ledger:doctor/tasks/t1",
        "behaviour": "All front doors share the preferred stored login.",
        "semgrep_rule": "feature-login",
        "behaviour_test": "tests/test_login.py::test_shared",
        "live_check": ["bash", "scripts/live.sh"],
    }
    path = tmp_path / "feature-map.json"
    path.write_text(json.dumps({"features": [entry]}))
    return tmp_path, path, entry


def _rewrite(path, *entries):
    path.write_text(json.dumps({"features": list(entries)}))


def _outside_operator(origin, **fields):
    return {
        "id": "login",
        "origin": origin,
        "ledger_item": "ledger:rig/tasks/rpin1",
        "behaviour": "Front doors share a login.",
        "seam": "launch",
        "tests": ["tests/test_login.py::test_shared"],
        **fields,
    }


def test_complete_feature_map_passes(mapped_repo):
    root, path, _ = mapped_repo
    assert feature_map.validate(root, path) == 1


def test_missing_behaviour_test_fails_with_the_feature_name(mapped_repo):
    root, path, entry = mapped_repo
    entry["behaviour_test"] = "tests/test_login.py::test_missing"
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="login.*missing behaviour test"):
        feature_map.validate(root, path)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("id", "^feature: requires id$"),
        ("origin", "^login: origin must be one of operator, regression, inventory$"),
        ("seam", "^login: requires seam$"),
        ("operator_words", "^login: requires operator_words$"),
        ("ordered_at", "^login: requires ordered_at$"),
        ("ledger_item", "^login: requires ledger_item$"),
        ("behaviour", "^login: requires behaviour$"),
        ("semgrep_rule", "^login: requires semgrep_rule$"),
        ("behaviour_test", "^login: requires tests$"),
        ("live_check", "^login: requires live_check argument list$"),
    ],
)
def test_every_link_and_operator_provenance_is_required(mapped_repo, field, message):
    root, path, entry = mapped_repo
    del entry[field]
    _rewrite(path, entry)
    with pytest.raises(ValueError, match=message):
        feature_map.validate(root, path)


@pytest.mark.parametrize("words", ["", "   ", None, 17])
def test_a_feature_without_operator_words_is_rejected(mapped_repo, words):
    root, path, entry = mapped_repo
    entry["operator_words"] = words
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="operator_words"):
        feature_map.validate(root, path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ordered_at", "2026-02-30", "ordered_at"),
        ("ordered_at", "tomorrow", "ordered_at"),
        ("ledger_item", "agent memory", "ledger_item"),
        ("semgrep_rule", "feature-missing", "missing rule"),
        ("behaviour_test", "tests/gone.py::test_shared", "missing behaviour test"),
        ("live_check", ["bash", "scripts/gone.sh"], "missing live check"),
        ("live_check", ["python", "-m", "scripts.gone"], "missing live check"),
        ("live_check", ["python", "scripts/gone.py"], "missing live check"),
        ("live_check", ["made-up-check"], "missing live check"),
        ("live_check", "bash scripts/live.sh", "live_check"),
        ("live_check", [], "live_check"),
        ("live_check", ["bash", "../outside.sh"], "live check"),
    ],
)
def test_dangling_or_invalid_references_fail(mapped_repo, field, value, message):
    root, path, entry = mapped_repo
    entry[field] = value
    _rewrite(path, entry)
    with pytest.raises(ValueError, match=message):
        feature_map.validate(root, path)


def test_duplicate_feature_ids_fail(mapped_repo):
    root, path, entry = mapped_repo
    _rewrite(path, entry, entry)
    with pytest.raises(ValueError, match="^duplicate feature login$"):
        feature_map.validate(root, path)


def test_an_unmapped_feature_rule_fails_unless_the_map_lists_it(mapped_repo):
    root, path, entry = mapped_repo
    (root / ".semgrep/features/other.yaml").write_text("rules:\n  - id: feature-orphan\n  - id: feature-stray\n")
    with pytest.raises(ValueError, match=r"^unmapped rule: \['feature-orphan', 'feature-stray'\]$"):
        feature_map.validate(root, path)
    path.write_text(json.dumps({"features": [entry], "unmapped_rules": ["feature-orphan", "feature-stray"]}))
    assert feature_map.validate(root, path) == 1


@pytest.mark.parametrize("unmapped", [["feature-login"], ["feature-gone"], ["feature-gone", "feature-login"]])
def test_mapped_or_deleted_rules_must_leave_the_unmapped_list(mapped_repo, unmapped):
    root, path, entry = mapped_repo
    path.write_text(json.dumps({"features": [entry], "unmapped_rules": unmapped}))
    with pytest.raises(
        ValueError, match=rf"^remove mapped or deleted rules from unmapped_rules: {re.escape(str(sorted(unmapped)))}$"
    ):
        feature_map.validate(root, path)


@pytest.mark.parametrize("unmapped", ["feature-login", [17], {"feature-login": 1}])
def test_unmapped_rules_must_be_a_list_of_rule_ids(mapped_repo, unmapped):
    root, path, entry = mapped_repo
    path.write_text(json.dumps({"features": [entry], "unmapped_rules": unmapped}))
    with pytest.raises(ValueError, match="^unmapped_rules must be a list of rule ids$"):
        feature_map.validate(root, path)


def test_an_unmapped_feature_test_fails(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_orphan.py").write_text(
        'import pytest\n@pytest.mark.feature("orphan")\ndef test_other():\n    assert True\n'
    )
    with pytest.raises(ValueError, match=r"^unmapped feature test: \['tests/test_orphan.py::test_other'\]$"):
        feature_map.validate(root, path)


def test_a_marked_test_no_entry_lists_is_unmapped(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_more.py").write_text('import pytest\n@pytest.mark.feature("login")\ndef test_a():\n    pass\n')
    with pytest.raises(ValueError, match=r"^unmapped feature test: \['tests/test_more.py::test_a'\]$"):
        feature_map.validate(root, path)


def test_referenced_test_must_carry_matching_feature_metadata(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_login.py").write_text("def test_shared():\n    assert True\n")
    with pytest.raises(ValueError, match="^login: behaviour test requires matching feature marker$"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("origin", ["", "operators", None, "incident"])
def test_an_unknown_origin_fails(mapped_repo, origin):
    root, path, entry = mapped_repo
    _rewrite(path, {**entry, "origin": origin})
    with pytest.raises(ValueError, match="^login: origin must be one of operator, regression, inventory$"):
        feature_map.validate(root, path)


def test_an_inventory_entry_needs_only_its_behaviour_seam_and_tests(mapped_repo):
    root, path, _ = mapped_repo
    entry = _outside_operator("inventory")
    del entry["ledger_item"]
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


def test_a_regression_with_its_incident_passes_without_rule_or_live_check(mapped_repo):
    root, path, _ = mapped_repo
    _rewrite(path, _outside_operator("regression"))
    assert feature_map.validate(root, path) == 1


@pytest.mark.parametrize("ledger_item", [None, "", "agent memory"])
def test_a_regression_needs_the_ledger_item_of_its_incident(mapped_repo, ledger_item):
    root, path, _ = mapped_repo
    entry = _outside_operator("regression", ledger_item=ledger_item)
    if ledger_item is None:
        del entry["ledger_item"]
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="^login: (requires|invalid) ledger_item$"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("origin", ["regression", "inventory"])
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("semgrep_rule", "feature-missing", "^login: missing rule feature-missing$"),
        ("semgrep_rule", "", "^login: requires semgrep_rule$"),
        ("live_check", ["bash", "scripts/gone.sh"], "^login: missing live check"),
        ("live_check", "bash scripts/live.sh", "^login: requires live_check argument list$"),
        ("ledger_item", "agent memory", "^login: invalid ledger_item$"),
    ],
)
def test_optional_links_outside_operator_are_checked_when_present(mapped_repo, origin, field, value, message):
    root, path, _ = mapped_repo
    _rewrite(path, _outside_operator(origin, **{field: value}))
    with pytest.raises(ValueError, match=message):
        feature_map.validate(root, path)


@pytest.mark.parametrize("origin", ["regression", "inventory"])
def test_present_rule_and_live_check_pass_outside_operator(mapped_repo, origin):
    root, path, _ = mapped_repo
    entry = _outside_operator(origin, semgrep_rule="feature-login", live_check=["bash", "scripts/live.sh"])
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


def test_every_listed_test_must_exist_and_carry_the_marker(mapped_repo):
    root, path, entry = mapped_repo
    (root / "tests/test_more.py").write_text(
        'import pytest\n@pytest.mark.feature("login")\ndef test_a():\n    pass\ndef test_b():\n    pass\n'
    )
    _rewrite(path, {**entry, "tests": ["tests/test_more.py::test_a"]})
    assert feature_map.validate(root, path) == 1
    _rewrite(path, {**entry, "tests": ["tests/test_more.py::test_a", "tests/test_more.py::test_b"]})
    with pytest.raises(ValueError, match="^login: behaviour test requires matching feature marker$"):
        feature_map.validate(root, path)
    _rewrite(path, {**entry, "tests": ["tests/test_more.py::test_gone"]})
    with pytest.raises(ValueError, match="^login: missing behaviour test tests/test_more.py::test_gone$"):
        feature_map.validate(root, path)


def test_tests_alone_replace_the_single_behaviour_test(mapped_repo):
    root, path, entry = mapped_repo
    del entry["behaviour_test"]
    _rewrite(path, {**entry, "tests": ["tests/test_login.py::test_shared"]})
    assert feature_map.validate(root, path) == 1


@pytest.mark.parametrize("tests", ["tests/test_login.py::test_shared", [""], ["  "], [17], {"a": 1}])
def test_tests_must_be_a_list_of_node_ids(mapped_repo, tests):
    root, path, entry = mapped_repo
    _rewrite(path, {**entry, "tests": tests})
    with pytest.raises(ValueError, match="^login: tests must be a list of test node ids$"):
        feature_map.validate(root, path)


def test_an_empty_test_list_fails(mapped_repo):
    root, path, entry = mapped_repo
    del entry["behaviour_test"]
    _rewrite(path, {**entry, "tests": []})
    with pytest.raises(ValueError, match="^login: requires tests$"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("command", [["python", "-m", "scripts.live"], ["python3", "scripts/live.py"]])
def test_python_live_targets_are_resolved_inside_the_repo(mapped_repo, command):
    root, path, entry = mapped_repo
    (root / "scripts/live.py").write_text("print('ok')\n")
    entry["live_check"] = command
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


def test_class_test_nodes_are_resolved(mapped_repo):
    root, path, entry = mapped_repo
    (root / "tests/test_login.py").write_text(
        'import pytest\nclass TestLogin:\n    @pytest.mark.feature("login")\n'
        "    def test_shared(self):\n        assert True\n"
    )
    entry["behaviour_test"] = "tests/test_login.py::TestLogin::test_shared"
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


@pytest.mark.parametrize(
    "source",
    [
        'class Helper:\n    @pytest.mark.feature("login")\n    def test_shared(self):\n        pass\n',
        'class TestLogin:\n    def __init__(self):\n        pass\n    @pytest.mark.feature("login")\n    def test_shared(self):\n        pass\n',
        'class TestLogin:\n    __test__ = False\n    @pytest.mark.feature("login")\n    def test_shared(self):\n        pass\n',
        '__test__ = False\n@pytest.mark.feature("login")\ndef test_shared():\n    pass\n',
    ],
)
def test_noncollectable_ast_names_are_not_suite_tests(mapped_repo, source):
    root, path, entry = mapped_repo
    (root / "tests/test_login.py").write_text("import pytest\n" + source)
    if source.startswith("class"):
        entry["behaviour_test"] = "tests/test_login.py::" + source.split()[1].rstrip(":") + "::test_shared"
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="missing behaviour test"):
        feature_map.validate(root, path)


def test_suffix_named_feature_tests_cannot_escape_ownership(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/login_test.py").write_text(
        'import pytest\n@pytest.mark.feature("orphan")\ndef test_other():\n    pass\n'
    )
    with pytest.raises(ValueError, match="unmapped feature test.*login_test.py::test_other"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("explicit_root", [False, True])
def test_cli_reports_the_complete_validated_feature_count(mapped_repo, monkeypatch, capsys, explicit_root):
    root, _, _ = mapped_repo
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "argv", ["feature-map", *(["--root", str(root)] if explicit_root else [])])
    feature_map.main()
    assert capsys.readouterr().out == "Feature map: 1 features, all rule, test and live check links exist\n"


def test_duplicate_rule_ids_cannot_hide_a_rule(mapped_repo):
    root, path, _ = mapped_repo
    (root / ".semgrep/features/copy.yml").write_text("rules:\n  - id: feature-login\n")
    with pytest.raises(ValueError, match="^duplicate feature rule id$"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("entries", [[], "features"])
def test_feature_map_requires_an_entry_list(mapped_repo, entries):
    root, path, _ = mapped_repo
    path.write_text(json.dumps({"features": entries}))
    with pytest.raises(ValueError, match="^features must be a nonempty list$"):
        feature_map.validate(root, path)


@pytest.mark.parametrize(
    "command",
    [
        ["bash"],
        ["python"],
        ["python", "-m"],
        ["python", "-m", "../foreign"],
        ["python", "-m", "scripts.missing"],
        [None],
        ["bash", ""],
    ],
)
def test_live_check_must_identify_a_repository_command(mapped_repo, command):
    root, path, entry = mapped_repo
    entry["live_check"] = command
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="live.check"):
        feature_map.validate(root, path)


def test_executable_repo_script_can_be_a_live_check(mapped_repo):
    root, path, entry = mapped_repo
    script = root / "scripts/live.sh"
    script.chmod(0o755)
    entry["live_check"] = ["./scripts/live.sh"]
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1
    script.chmod(0o644)
    with pytest.raises(ValueError, match="missing live check"):
        feature_map.validate(root, path)


def test_python_package_entry_point_can_be_a_live_check(mapped_repo):
    root, path, entry = mapped_repo
    package = root / "scripts/live"
    package.mkdir()
    (package / "__main__.py").write_text("print('ok')\n")
    entry["live_check"] = ["python3", "-m", "scripts.live"]
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


def test_only_rule_documents_are_discovered_alongside_nested_fixtures(mapped_repo):
    root, path, _ = mapped_repo
    rules = root / ".semgrep/features"
    nested = rules / "nested"
    nested.mkdir()
    (rules / "README.md").write_text("Feature rules and fixtures")
    (nested / "login.test.yaml").write_text("example: regression\n---\nexample: passing\n")
    (nested / "login.test.yml").write_text("example: regression\n---\nexample: passing\n")
    assert feature_map.validate(root, path) == 1


def test_other_pytest_decorators_are_not_feature_metadata(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_login.py").write_text(
        'import pytest\n@pytest.mark.feature("login")\n@pytest.mark.parametrize("value", [1])\n'
        "@pytest.mark.unit\ndef test_shared(value):\n    assert value\n"
    )
    assert feature_map.validate(root, path) == 1


def test_uppercase_python_module_names_are_valid_live_targets(mapped_repo):
    root, path, entry = mapped_repo
    (root / "scripts/Live.py").write_text("print('ok')\n")
    entry["live_check"] = ["python", "-m", "scripts.Live"]
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


def test_non_string_live_arguments_cannot_enter_the_doctor_command(mapped_repo):
    root, path, entry = mapped_repo
    entry["live_check"] = ["bash", 42]
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="live_check"):
        feature_map.validate(root, path)


def test_nested_suite_classes_keep_the_full_test_node_id(mapped_repo):
    root, path, entry = mapped_repo
    (root / "tests/test_login.py").write_text(
        "import pytest\nclass TestOuter:\n    class TestInner:\n"
        '        @pytest.mark.feature("login")\n        async def test_shared(self):\n            pass\n'
    )
    entry["behaviour_test"] = "tests/test_login.py::TestOuter::TestInner::test_shared"
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


def test_support_modules_and_constants_do_not_hide_feature_tests(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/aaa_support.py").write_text("VALUE = 1\n")
    source = root / "tests/test_login.py"
    source.write_text("VALUE = 1\n" + source.read_text())
    assert feature_map.validate(root, path) == 1


def test_a_suffix_named_suite_node_can_be_mapped(mapped_repo):
    root, path, entry = mapped_repo
    (root / "tests/test_login.py").rename(root / "tests/login_test.py")
    entry["behaviour_test"] = "tests/login_test.py::test_shared"
    _rewrite(path, entry)
    assert feature_map.validate(root, path) == 1


def test_a_class_with_new_is_not_a_suite_test(mapped_repo):
    root, path, entry = mapped_repo
    (root / "tests/test_login.py").write_text(
        "import pytest\nclass TestLogin:\n    def __new__(cls):\n        pass\n"
        '    @pytest.mark.feature("login")\n    def test_shared(self):\n        pass\n'
    )
    entry["behaviour_test"] = "tests/test_login.py::TestLogin::test_shared"
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="missing behaviour test"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("marker", ["None", "17", '""', '"  "'])
def test_a_feature_marker_cannot_discard_its_ownership(mapped_repo, marker):
    root, path, _ = mapped_repo
    (root / "tests/test_orphan.py").write_text(
        f"import pytest\n@pytest.mark.feature({marker})\ndef test_other():\n    pass\n"
    )
    with pytest.raises(ValueError, match="feature marker requires an id"):
        feature_map.validate(root, path)


def test_unmarked_suite_tests_do_not_need_feature_entries(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_unrelated.py").write_text("def test_other():\n    pass\n")
    assert feature_map.validate(root, path) == 1


def test_the_repository_feature_map_is_valid():
    root = Path(__file__).resolve().parents[1]
    assert feature_map.validate(root, root / "feature-map.json") >= 1


@pytest.mark.parametrize("ordered_at", ["20261025", "2026-10-25x", " 2026-10-25"])
def test_ordered_at_takes_only_the_dashed_date_form(mapped_repo, ordered_at):
    root, path, entry = mapped_repo
    _rewrite(path, {**entry, "ordered_at": ordered_at})
    with pytest.raises(ValueError, match="^login: invalid ordered_at$"):
        feature_map.validate(root, path)


def test_an_order_date_outside_operator_needs_its_ledger_item(mapped_repo):
    root, path, _ = mapped_repo
    entry = _outside_operator("inventory", ordered_at="2026-10-25")
    del entry["ledger_item"]
    _rewrite(path, entry)
    with pytest.raises(ValueError, match="^login: requires ledger_item$"):
        feature_map.validate(root, path)
    _rewrite(path, _outside_operator("inventory", ordered_at="2026-02-30"))
    with pytest.raises(ValueError, match="^login: invalid ordered_at$"):
        feature_map.validate(root, path)
    _rewrite(path, _outside_operator("inventory", ordered_at="2026-10-25"))
    assert feature_map.validate(root, path) == 1


@pytest.mark.parametrize(
    ("ledger_item", "valid"),
    [
        ("ledger:rig/phases/fmap", True),
        ("ledger:rig/comments/c-1", True),
        ("ledger:rig/chat/m_2", True),
        ("ledger:rig/tasks/t1 extra", False),
        ("see ledger:rig/tasks/t1", False),
        ("ledger:rig/notes/t1", False),
    ],
)
def test_ledger_items_take_the_whole_ledger_form(mapped_repo, ledger_item, valid):
    root, path, entry = mapped_repo
    _rewrite(path, {**entry, "ledger_item": ledger_item})
    if valid:
        assert feature_map.validate(root, path) == 1
    else:
        with pytest.raises(ValueError, match="^login: invalid ledger_item$"):
            feature_map.validate(root, path)


def test_a_live_check_outside_the_repository_fails_even_when_it_exists(mapped_repo):
    root, path, entry = mapped_repo
    (root.parent / "outside.sh").write_text("exit 0\n")
    _rewrite(path, {**entry, "live_check": ["bash", "../outside.sh"]})
    with pytest.raises(ValueError, match=r"^login: missing live check \['bash', '../outside.sh'\]$"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("marker", ["", "id='login'", "LOGIN", "'log' + 'in'"])
def test_a_feature_marker_needs_a_literal_id_argument(mapped_repo, marker):
    root, path, _ = mapped_repo
    (root / "tests/test_orphan.py").write_text(
        f"import pytest\nLOGIN = 'login'\n@pytest.mark.feature({marker})\ndef test_other():\n    pass\n"
    )
    with pytest.raises(ValueError, match="^tests/test_orphan.py::test_other: feature marker requires an id$"):
        feature_map.validate(root, path)


def test_a_feature_marker_on_a_class_is_refused(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_orphan.py").write_text(
        'import pytest\n@pytest.mark.feature("login")\nclass TestOrphan:\n    def test_other(self):\n        pass\n'
    )
    with pytest.raises(
        ValueError, match="^tests/test_orphan.py::TestOrphan: feature marker belongs on test functions$"
    ):
        feature_map.validate(root, path)


def _gh(tmp_path, body):
    gh = tmp_path / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, sys\n{body}")
    gh.chmod(0o755)


def _live(tmp_path, *args):
    script = Path(__file__).resolve().parents[1] / "scripts/feature_checks/semgrep-feature-tests.sh"
    return subprocess.run(
        ["bash", str(script), *args],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )


STEP = "Operator feature regressions and passing fixtures"


def test_live_check_default_selects_the_latest_completed_pull_request_run(tmp_path):
    _gh(
        tmp_path,
        'if "/workflows/test.yml/runs?" in sys.argv[2]:\n'
        '    assert sys.argv[1:] == ["api", "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs?event=pull_request&status=completed&per_page=1", "--jq", \'.workflow_runs[0].id // error("No completed pull request Tests run")\']\n'
        '    print("123")\nelse:\n'
        '    assert sys.argv[1:] == ["api", "repos/the-cloud-clockwork/agentihooks/actions/runs/123/jobs?per_page=100", "--paginate"]\n'
        f'    print(json.dumps({{"jobs": [{{"conclusion": "success", "steps": [{{"name": "{STEP}", "conclusion": "success"}}]}}]}}))\n',
    )
    result = _live(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"


@pytest.mark.parametrize(
    ("jobs", "success"),
    [
        ([{"conclusion": "success", "steps": [{"name": STEP, "conclusion": "success"}]}], True),
        ([], False),
        ([{"conclusion": "failure", "steps": [{"name": STEP, "conclusion": "success"}]}], False),
        ([{"conclusion": "success", "steps": [{"name": STEP, "conclusion": "skipped"}]}], False),
        ([{"conclusion": "success", "steps": [{"name": "Other check", "conclusion": "success"}]}], False),
    ],
)
def test_live_check_requires_the_deployed_feature_job_and_step(tmp_path, jobs, success):
    _gh(
        tmp_path,
        'assert sys.argv[1:] == ["api", "repos/the-cloud-clockwork/agentihooks/actions/runs/123/jobs?per_page=100", "--paginate"]\n'
        f"print({json.dumps(json.dumps({'jobs': jobs}))})\n",
    )
    result = _live(tmp_path, "123")
    assert (result.returncode == 0) is success, result.stderr


@pytest.mark.parametrize(
    "source",
    [
        '@pytest.mark.feature("login")\n@pytest.mark.skip(reason="later")\ndef test_shared():\n    assert True\n',
        '@pytest.mark.feature("login")\n@pytest.mark.skipif(True, reason="x")\ndef test_shared():\n    assert True\n',
        '@pytest.mark.feature("login")\n@pytest.mark.xfail\ndef test_shared():\n    assert True\n',
        '@pytest.mark.feature("login")\ndef test_shared():\n    pytest.importorskip("missing")\n    assert True\n',
        '@pytest.mark.feature("login")\ndef test_shared():\n    pytest.skip("later")\n',
        'pytestmark = pytest.mark.skip\n@pytest.mark.feature("login")\ndef test_shared():\n    assert True\n',
        'class TestLogin:\n    @pytest.mark.feature("login")\n    def test_shared(self):\n        self.skipTest("x")\n',
    ],
)
def test_a_disabled_mapped_test_fails(mapped_repo, source):
    root, path, entry = mapped_repo
    node = "tests/test_login.py::" + ("TestLogin::test_shared" if "class" in source else "test_shared")
    _rewrite(path, {**entry, "behaviour_test": node})
    (root / "tests/test_login.py").write_text("import pytest\n" + source)
    with pytest.raises(ValueError, match=re.escape(f"mapped test is skipped, xfailed or returns early: ['{node}']")):
        feature_map.validate(root, path)
    (root / "tests/test_login.py").write_text(
        "import pytest\n" + re.sub(r"pytest\.(mark\.)?(skipif|skip|xfail|importorskip)|self\.skipTest", "str", source)
    )
    assert feature_map.validate(root, path) == 1


def _base(tmp_path, features, retired=()):
    base = tmp_path / "base-map.json"
    base.write_text(json.dumps({"features": features, "retired": list(retired)}))
    return base


RETIRED = {"id": "logout", "reason": "Merged into login", "ledger_item": "ledger:rig/tasks/t2"}


def test_removing_a_feature_needs_a_retired_record(mapped_repo):
    root, path, entry = mapped_repo
    base = _base(root, [entry, {**entry, "id": "logout"}])
    with pytest.raises(ValueError, match=re.escape("removed feature needs a retired record: ['logout']")):
        feature_map.validate(root, path, base)
    path.write_text(json.dumps({"features": [entry], "retired": [RETIRED]}))
    assert feature_map.validate(root, path, base) == 1


@pytest.mark.parametrize("field", ["id", "reason", "ledger_item"])
def test_a_retired_record_needs_its_id_reason_and_ledger_item(mapped_repo, field):
    root, path, entry = mapped_repo
    path.write_text(json.dumps({"features": [entry], "retired": [{**RETIRED, field: ""}]}))
    with pytest.raises(ValueError, match=f"requires {field}$"):
        feature_map.validate(root, path)


def test_a_retired_record_takes_a_whole_ledger_item(mapped_repo):
    root, path, entry = mapped_repo
    path.write_text(json.dumps({"features": [entry], "retired": [{**RETIRED, "ledger_item": "t2"}]}))
    with pytest.raises(ValueError, match="^logout: invalid retired ledger_item$"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("retired", [{"id": "x"}, [RETIRED, RETIRED], ["logout"]])
def test_retired_must_be_a_list_of_distinct_records(mapped_repo, retired):
    root, path, entry = mapped_repo
    path.write_text(json.dumps({"features": [entry], "retired": retired}))
    with pytest.raises(ValueError, match="retired"):
        feature_map.validate(root, path)


def test_a_retired_id_is_never_reused(mapped_repo):
    root, path, entry = mapped_repo
    path.write_text(json.dumps({"features": [entry], "retired": [{**RETIRED, "id": "login"}]}))
    with pytest.raises(ValueError, match=re.escape("retired feature id reused: ['login']")):
        feature_map.validate(root, path)


def test_a_retired_record_cannot_be_dropped(mapped_repo):
    root, path, entry = mapped_repo
    base = _base(root, [entry], [RETIRED])
    with pytest.raises(ValueError, match=re.escape("retired record removed: ['logout']")):
        feature_map.validate(root, path, base)
    path.write_text(json.dumps({"features": [entry], "retired": [RETIRED]}))
    assert feature_map.validate(root, path, base) == 1


def test_removing_a_mapped_test_without_changing_its_entry_fails(mapped_repo):
    root, path, entry = mapped_repo
    base = _base(root, [entry])
    assert feature_map.validate(root, path, base) == 1
    (root / "tests/test_login.py").write_text("import pytest\n")
    with pytest.raises(ValueError, match="^login: missing behaviour test tests/test_login.py::test_shared$"):
        feature_map.validate(root, path, base)


def test_a_base_without_a_map_grades_the_head_alone(mapped_repo):
    root, path, _ = mapped_repo
    assert feature_map.validate(root, path, root / "absent.json") == 1


def test_cli_grades_against_the_base_map(mapped_repo, monkeypatch, capsys):
    root, path, entry = mapped_repo
    base = root / "base"
    base.mkdir()
    (base / "feature-map.json").write_text(json.dumps({"features": [entry, {**entry, "id": "logout"}]}))
    monkeypatch.setattr(sys, "argv", ["feature_map", "--root", str(root), "--base", str(base)])
    with pytest.raises(ValueError, match="logout"):
        feature_map.main()
    path.write_text(json.dumps({"features": [entry], "retired": [RETIRED]}))
    feature_map.main()
    assert "1 features" in capsys.readouterr().out


def test_lint_grades_the_feature_map_from_the_protected_grader_against_the_base():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    step = next(
        step for step in workflow["jobs"]["lint"]["steps"] if step.get("name") == "Hold the mapped feature tests"
    )
    assert step["if"] == "${{ !cancelled() }}"
    assert 'cd "$RUNNER_TEMP/grader"' in step["run"]
    assert (
        step["run"]
        .strip()
        .endswith('python -m scripts.feature_map --root "$GITHUB_WORKSPACE" --base "$RUNNER_TEMP/base"')
    )
