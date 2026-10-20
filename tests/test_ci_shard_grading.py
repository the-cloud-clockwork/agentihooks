from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _jobs():
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]


def test_shard_graders_check_out_event_base_before_installing():
    steps = _jobs()["shard-check"]["steps"]
    checkout = steps[0]
    assert checkout["uses"] == "actions/checkout@v4"
    legacy = (
        "${{ github.event.pull_request.base.ref == 'main' && github.sha || "
        "github.event_name == 'pull_request' && 'dev' || github.event.pull_request.base.sha || github.event.merge_group.base_sha || "
        "github.event.before || (inputs.base == 'origin/dev' && 'dev' || inputs.base) }}"
    )
    promotion = legacy.replace(
        "github.event.before ||", "github.event_name == 'push' && github.sha || github.event.before ||"
    )
    assert checkout["with"]["ref"] in (legacy, promotion)
    assert checkout["with"]["persist-credentials"] is False
    assert sum(step.get("uses", "").startswith("actions/checkout@") for step in steps) == 1


def test_shard_grader_never_restores_an_environment_written_by_head_tests():
    steps = _jobs()["shard-check"]["steps"]
    assert not any(step.get("uses", "").startswith("actions/cache") for step in steps)
    install = next(step for step in steps if step.get("name") == "Install grader dependencies")
    assert install["run"] == 'python -m pip install "pytest>=8.0"'
    assert not any(step.get("name") == "Use the test environment" for step in steps)


def test_head_collection_is_uploaded_once_per_interpreter_and_downloaded_as_data():
    jobs = _jobs()
    split = jobs["split"]["steps"]
    collect = next(step for step in split if step.get("name") == "Adopt latest dev durations")
    upload = next(step for step in split if step.get("name") == "Upload collected tests")
    assert upload["if"] == "github.event.pull_request.base.ref != 'main'"
    assert collect["run"].endswith(" --collected collected.json")
    assert "--shard" not in collect["run"]
    versions = jobs["split"]["strategy"]["matrix"]["python-version"]
    joint = "${{ fromJSON(github.event_name == 'merge_group' && '[\"3.11\"]' || '[\"3.11\",\"3.12\"]') }}"
    assert jobs["test-count"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    assert jobs["unit"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    assert versions in (["3.11", "3.12"], joint)
    if versions == joint:
        artifacts = {step.get("with", {}).get("name"): step for step in split if step.get("uses")}
        assert artifacts["early-collected-3.12"]["with"]["path"] == "queue-input/3.12"
        assert artifacts["collected-3.12"]["with"]["path"] == "queue-input/3.12/collected.json"
        assert artifacts["collected-3.12"]["with"]["if-no-files-found"] == "error"
    assert upload["with"]["name"] == "collected-${{ matrix.python-version }}"
    assert upload["with"]["path"] == "collected.json"
    assert upload["with"]["if-no-files-found"] == "error"
    assert split.index(collect) < split.index(upload)
    steps = jobs["shard-check"]["steps"]
    download = next(step for step in steps if step.get("name") == "Download collected tests")
    check = next(step for step in steps if step.get("run", "").startswith("python -m tests.shard_check"))
    assert download["with"]["name"] == upload["with"]["name"]
    assert f"--collected {download['with']['path']}/collected.json" in check["run"]
    assert steps.index(download) < steps.index(check)


def test_shard_grading_keeps_head_data_separate_from_base_modules():
    steps = _jobs()["shard-check"]["steps"]
    commands = [step["run"] for step in steps if step.get("run", "").startswith("python -m tests.shard_")]
    assert commands == [
        "python -m tests.shard_check shard-durations/*/durations.json --collected collected-data/collected.json $REUSED",
        'python -m tests.shard_summary --version "$PYTHON_VERSION" --durations shard-durations/*/durations.json \\\n'
        '  --collected collected-data/collected.json --plan "$PLAN" --flakes shard-flakes/*/flakes.jsonl'
        ' | tee -a "$GITHUB_STEP_SUMMARY"\n',
        "python -m tests.shard_check shard-durations/*/durations.json",
        "python -m tests.shard_budget $REPORT shard-durations/*/durations.json",
    ]


def test_release_grading_preserves_legacy_collection_and_dev_never_uses_it():
    steps = _jobs()["shard-check"]["steps"]
    trusted = next(step for step in steps if step.get("name") == "Check every collected test ran in exactly one shard")
    legacy = next(step for step in steps if step.get("name") == "Check release shards with legacy collection")
    install = next(step for step in steps if step.get("name") == "Install legacy grader dependencies")
    download = next(step for step in steps if step.get("name") == "Download collected tests")
    assert legacy["if"] == install["if"] == "github.event.pull_request.base.ref == 'main'"
    assert trusted["if"] == download["if"] == "github.event.pull_request.base.ref != 'main'"
    assert legacy["run"] == "python -m tests.shard_check shard-durations/*/durations.json"
