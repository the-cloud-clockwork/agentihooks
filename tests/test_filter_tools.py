"""Filters through the condition tools: the filter kind on set, its schema check,
the operator's filter wording, the finder folders, and list, show and clear."""

import json
from pathlib import Path

import pytest

from hooks.context import conditions
from tests import test_condition_tools as base

pytestmark = pytest.mark.unit

SID = base.SID
bundle = base.bundle
tools = base.tools

FILTER = "finders:\n  - regex: TODO\n    reason: leftover marker\nmode: finders\naction: flag\nmax_rounds: 2\n"


def _create(**overrides):
    args = {
        "step": "pre",
        "matcher": "write",
        "name": "slop",
        "script": FILTER,
        "session_id": SID,
        "language": "filter",
    }
    return conditions.create_condition(**{**args, **overrides})


class TestCreateFilter:
    def test_a_valid_filter_is_written_into_the_bundle(self, bundle):
        conditions.arm_gate(SID)
        result = _create()
        path = Path(result["path"])
        assert path == bundle / ".claude" / "conditions" / "pre-write-slop.filter.yaml"
        assert (result["layer"], result["file"], result["step"], result["matcher"]) == (
            "bundle",
            "pre-write-slop.filter.yaml",
            "pre",
            "write",
        )
        assert path.read_text() == FILTER
        assert [e["file"] for e in conditions.matching("pre", "Write", {"file_path": "/x"})] == [result["file"]]

    def test_profile_and_directory_scopes(self, bundle, tmp_path):
        repo = base._git_repo(tmp_path / "work")
        conditions.arm_gate(SID)
        profile = _create(scope="profile", profile="beta")
        directory = _create(scope="directory", cwd=str(repo))
        assert Path(profile["path"]).parent == bundle / "profiles" / "beta" / ".claude" / "conditions"
        assert Path(directory["path"]) == repo / ".agentihooks" / "conditions" / "pre-write-slop.filter.yaml"

    def test_refused_without_the_operator_gate(self, bundle):
        with pytest.raises(conditions.ConditionError, match="operator's own prompt"):
            _create()
        assert not (bundle / ".claude" / "conditions").exists()

    @pytest.mark.parametrize(
        "script, error",
        [
            ("mode: sometimes\n", "invalid filter: mode must be one of finders, classifier, both, got 'sometimes'"),
            ("colour: red\n", "invalid filter: unknown keys: colour"),
            ("- a\n- b\n", "invalid filter: a filter must be a mapping of keys"),
            ("mode: [unclosed\n", "invalid filter: the body is not YAML"),
            ("   \n", "script is empty"),
        ],
    )
    def test_an_invalid_body_is_refused_with_the_schema_error(self, bundle, script, error):
        conditions.arm_gate(SID)
        with pytest.raises(conditions.ConditionError) as raised:
            _create(script=script)
        assert str(raised.value).startswith(error)
        assert not (bundle / ".claude" / "conditions").exists()

    def test_a_filter_is_not_executable_and_takes_no_shebang(self, bundle):
        conditions.arm_gate(SID)
        path = Path(_create(script="mode: classifier").get("path"))
        assert path.read_text() == "mode: classifier\n"
        assert path.stat().st_mode & 0o777 == 0o644

    def test_a_filter_cannot_run_async(self, bundle):
        conditions.arm_gate(SID)
        with pytest.raises(conditions.ConditionError, match="a filter runs in process and cannot be async"):
            _create(run_async=True)

    def test_the_language_error_names_the_filter_kind(self, bundle):
        conditions.arm_gate(SID)
        with pytest.raises(conditions.ConditionError, match="language must be one of bash, python, filter"):
            _create(language="ruby")


class TestFilterSignal:
    @pytest.mark.parametrize(
        "prompt",
        [
            "set a ui slop filter",
            "add a filter for ledger writes",
            "create the classifier for inbox sends",
            "update the ledger text filter",
            "remove the ui slop filter",
            "set the front end slop filter",
            "add filters for the judge",
        ],
    )
    def test_arms(self, prompt):
        assert conditions.contains_condition_signal(prompt)

    @pytest.mark.parametrize(
        "prompt",
        [
            "don't add a filter for this",
            "what does the filter do?",
            "the classifier answered no",
            "update the docs about filters",
        ],
    )
    def test_does_not_arm(self, prompt):
        assert not conditions.contains_condition_signal(prompt)


class TestFinderFolders:
    @pytest.mark.parametrize(
        "tool, tool_input",
        [
            ("Write", {"file_path": "/r/.agentihooks/conditions/_finders/slop.py", "content": "print('[]')"}),
            ("Edit", {"file_path": "/b/.claude/conditions/_finders/slop.sh", "old_string": "a", "new_string": "b"}),
            ("Bash", {"command": "cp /tmp/x .agentihooks/conditions/_finders/slop.py"}),
            ("Bash", {"command": "rm ~/.agentihooks/conditions/_finders/slop.py"}),
        ],
    )
    def test_a_finder_write_needs_the_operator_gate(self, tool, tool_input):
        assert conditions.write_guard(tool, tool_input, SID) == conditions.GATE_MESSAGE
        conditions.arm_gate(SID)
        assert conditions.write_guard(tool, tool_input, SID) is None

    def test_a_finder_write_into_a_repo_on_its_default_branch_needs_the_gate(self, bundle, tmp_path):
        repo = base._git_repo(tmp_path / "work")
        write = {"file_path": f"{repo}/.agentihooks/conditions/_finders/slop.py", "content": "print('[]')"}
        assert conditions.write_guard("Write", write, SID, str(repo)) == conditions.GATE_MESSAGE


class TestInventory:
    def test_list_shows_mode_action_and_rounds(self, bundle):
        conditions.arm_gate(SID)
        _create()
        _create(name="defaults", script="intent: no slop\n")
        found = {e["file"]: e.get("filter") for e in conditions.inventory()["conditions"]}
        assert found == {
            "pre-write-defaults.filter.yaml": {"mode": "both", "action": "send-back", "max_rounds": 3},
            "pre-write-slop.filter.yaml": {"mode": "finders", "action": "flag", "max_rounds": 2},
        }

    def test_a_condition_script_carries_no_filter_settings(self, bundle):
        conditions.arm_gate(SID)
        _create(language="bash", script="exit 0")
        (entry,) = conditions.inventory()["conditions"]
        assert entry["file"] == "pre-write-slop.sh" and "filter" not in entry

    def test_a_broken_filter_on_disk_is_listed_as_invalid(self, bundle):
        folder = bundle / ".claude" / "conditions"
        folder.mkdir(parents=True)
        (folder / "pre-write-broken.filter.yaml").write_text("mode: sometimes\n")
        result = conditions.inventory()
        assert result["conditions"] == []
        assert result["invalid"] == [
            {
                "path": str(folder / "pre-write-broken.filter.yaml"),
                "source": "bundle",
                "error": "mode must be one of finders, classifier, both, got 'sometimes'",
            }
        ]


class TestMCPFilterTools:
    def test_set_list_show_clear(self, bundle, tools, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = {"step": "pre", "matcher": "write", "name": "slop", "script": FILTER, "session_id": SID}
        refused = json.loads(tools["condition_set"](**args, language="filter"))
        assert refused["success"] is False and "operator's own prompt" in refused["error"]

        conditions.arm_gate(SID)
        invalid = json.loads(tools["condition_set"](**{**args, "script": "mode: sometimes\n"}, language="filter"))
        assert invalid == {
            "success": False,
            "error": "invalid filter: mode must be one of finders, classifier, both, got 'sometimes'",
        }

        created = json.loads(tools["condition_set"](**args, language="filter"))
        assert created["success"] and created["file"] == "pre-write-slop.filter.yaml"

        listed = json.loads(tools["condition_list"]())
        assert listed["conditions"] == [
            {
                "file": "pre-write-slop.filter.yaml",
                "step": "pre",
                "matcher": "write",
                "name": "slop.filter",
                "async": False,
                "source": "bundle",
                "path": created["path"],
                "order": 0,
                "filter": {"mode": "finders", "action": "flag", "max_rounds": 2},
            }
        ]

        shown = json.loads(tools["condition_show"]("pre-write-slop.filter.yaml"))
        assert shown["script"] == FILTER

        cleared = json.loads(tools["condition_clear"]("pre-write-slop.filter.yaml", SID))
        assert cleared == {"success": True, "removed": created["path"], "layer": "bundle"}
        assert json.loads(tools["condition_list"]())["conditions"] == []
