import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_complete_feature_map_passes(mapped_repo):
    root, path, _ = mapped_repo
    assert feature_map.validate(root, path) == 1


def test_missing_behaviour_test_fails_with_the_feature_name(mapped_repo):
    root, path, entry = mapped_repo
    entry["behaviour_test"] = "tests/test_login.py::test_missing"
    path.write_text(json.dumps({"features": [entry]}))
    with pytest.raises(ValueError, match="login.*missing behaviour test"):
        feature_map.validate(root, path)


@pytest.mark.parametrize(
    "field",
    ["id", "operator_words", "ordered_at", "ledger_item", "behaviour", "semgrep_rule", "behaviour_test", "live_check"],
)
def test_every_link_and_operator_provenance_is_required(mapped_repo, field):
    root, path, entry = mapped_repo
    del entry[field]
    path.write_text(json.dumps({"features": [entry]}))
    with pytest.raises(ValueError, match=field):
        feature_map.validate(root, path)


@pytest.mark.parametrize("words", ["", "   ", None, 17])
def test_a_feature_without_operator_words_is_rejected(mapped_repo, words):
    root, path, entry = mapped_repo
    entry["operator_words"] = words
    path.write_text(json.dumps({"features": [entry]}))
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
    path.write_text(json.dumps({"features": [entry]}))
    with pytest.raises(ValueError, match=message):
        feature_map.validate(root, path)


def test_duplicate_feature_ids_fail(mapped_repo):
    root, path, entry = mapped_repo
    path.write_text(json.dumps({"features": [entry, entry]}))
    with pytest.raises(ValueError, match="duplicate feature"):
        feature_map.validate(root, path)


def test_an_unmapped_feature_rule_fails(mapped_repo):
    root, path, _ = mapped_repo
    (root / ".semgrep/features/other.yaml").write_text("rules:\n  - id: feature-orphan\n")
    with pytest.raises(ValueError, match="unmapped rule.*feature-orphan"):
        feature_map.validate(root, path)


def test_an_unmapped_feature_test_fails(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_orphan.py").write_text(
        'import pytest\n@pytest.mark.feature("orphan")\ndef test_other():\n    assert True\n'
    )
    with pytest.raises(ValueError, match="unmapped feature test.*test_orphan.py::test_other"):
        feature_map.validate(root, path)


def test_referenced_test_must_carry_matching_feature_metadata(mapped_repo):
    root, path, _ = mapped_repo
    (root / "tests/test_login.py").write_text("def test_shared():\n    assert True\n")
    with pytest.raises(ValueError, match="login.*feature marker"):
        feature_map.validate(root, path)


@pytest.mark.parametrize("command", [["python", "-m", "scripts.live"], ["python3", "scripts/live.py"]])
def test_python_live_targets_are_resolved_inside_the_repo(mapped_repo, command):
    root, path, entry = mapped_repo
    (root / "scripts/live.py").write_text("print('ok')\n")
    entry["live_check"] = command
    path.write_text(json.dumps({"features": [entry]}))
    assert feature_map.validate(root, path) == 1


def test_class_test_nodes_are_resolved(mapped_repo):
    root, path, entry = mapped_repo
    (root / "tests/test_login.py").write_text(
        'import pytest\nclass TestLogin:\n    @pytest.mark.feature("login")\n'
        "    def test_shared(self):\n        assert True\n"
    )
    entry["behaviour_test"] = "tests/test_login.py::TestLogin::test_shared"
    path.write_text(json.dumps({"features": [entry]}))
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
    path.write_text(json.dumps({"features": [entry]}))
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
    assert capsys.readouterr().out == "Feature map: 1 features, all rule, behaviour and live check links exist\n"


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
    path.write_text(json.dumps({"features": [entry]}))
    with pytest.raises(ValueError, match="live.check"):
        feature_map.validate(root, path)


def test_executable_repo_script_can_be_a_live_check(mapped_repo):
    root, path, entry = mapped_repo
    script = root / "scripts/live.sh"
    script.chmod(0o755)
    entry["live_check"] = ["./scripts/live.sh"]
    path.write_text(json.dumps({"features": [entry]}))
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
    path.write_text(json.dumps({"features": [entry]}))
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
    path.write_text(json.dumps({"features": [entry]}))
    assert feature_map.validate(root, path) == 1


def test_non_string_live_arguments_cannot_enter_the_doctor_command(mapped_repo):
    root, path, entry = mapped_repo
    entry["live_check"] = ["bash", 42]
    path.write_text(json.dumps({"features": [entry]}))
    with pytest.raises(ValueError, match="live_check"):
        feature_map.validate(root, path)


@pytest.mark.parametrize(
    ("jobs", "success"),
    [
        (
            [
                {
                    "conclusion": "success",
                    "steps": [{"name": "Operator feature regressions and passing fixtures", "conclusion": "success"}],
                }
            ],
            True,
        ),
        ([], False),
        (
            [
                {
                    "conclusion": "failure",
                    "steps": [{"name": "Operator feature regressions and passing fixtures", "conclusion": "success"}],
                }
            ],
            False,
        ),
        (
            [
                {
                    "conclusion": "success",
                    "steps": [{"name": "Operator feature regressions and passing fixtures", "conclusion": "skipped"}],
                }
            ],
            False,
        ),
        ([{"conclusion": "success", "steps": [{"name": "Other check", "conclusion": "success"}]}], False),
    ],
)
def test_live_check_requires_the_deployed_feature_job_and_step(tmp_path, jobs, success):
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        'assert sys.argv[1:] == ["api", "repos/the-cloud-clockwork/agentihooks/actions/runs/123/jobs?per_page=100", "--paginate"]\n'
        f"print({json.dumps(json.dumps({'jobs': jobs}))})\n"
    )
    gh.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts/feature_checks/semgrep-feature-tests.sh"
    result = subprocess.run(
        ["bash", str(script), "123"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is success, result.stderr
