import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

_QUEUE = "github.event_name == 'merge_group'"
_BASELINE = "steps.dev-run.outputs.baseline == 'true'"


def _jobs():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _step(steps, name):
    return next(s for s in steps if s.get("name") == name)


def test_publisher_uploads_the_passed_dev_baseline_for_queue_runs():
    job = _jobs()["coverage-baseline"]
    save = _step(job["steps"], "Save the passed dev coverage baseline")
    upload = _step(job["steps"], "Publish the passed dev coverage baseline")
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["name"] == "coverage-baseline"
    assert upload["with"]["path"] == save["with"]["path"] == "~/coverage-baseline"
    assert upload["with"]["if-no-files-found"] == "error"


def test_queue_runs_mint_a_read_only_app_token_and_skip_the_dev_cache_lookup():
    job = _jobs()["durations"]
    lookup = job["steps"][0]
    mint = _step(job["steps"], "Mint the tcc main ci App token")
    assert lookup["if"] == "github.event_name != 'merge_group'"
    assert mint["if"] == _QUEUE
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"] == {
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-actions": "read",
    }
    assert job["permissions"] == {"contents": "read"}


@pytest.mark.parametrize(
    "artifact,path,republished,condition,hidden",
    [
        ("durations-merged", "~/dev-durations", "dev-durations", _QUEUE, True),
        ("coverage-baseline", "~/coverage-baseline", "dev-coverage-baseline", _BASELINE, None),
    ],
)
def test_queue_runs_restore_from_the_latest_passed_dev_push_on_the_app_token(
    artifact, path, republished, condition, hidden
):
    job = _jobs()["durations"]
    steps = job["steps"]
    mint = _step(steps, "Mint the tcc main ci App token")
    find = _step(steps, "Find the latest passed dev push run")
    download = next(
        s for s in steps if s.get("uses") == "actions/download-artifact@v4" and s["with"]["name"] == artifact
    )
    upload = next(
        s for s in steps if s.get("uses") == "actions/upload-artifact@v4" and s["with"]["name"] == republished
    )
    assert find["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert mint["id"] == "app-token"
    assert find["if"] == _QUEUE
    assert download["if"] == upload["if"] == condition
    assert download["with"] == {
        "name": artifact,
        "path": path,
        "run-id": "${{ steps.dev-run.outputs.id }}",
        "repository": "${{ github.repository }}",
        "github-token": "${{ steps.app-token.outputs.token }}",
    }
    assert upload["with"]["path"] == f"{path}/"
    assert upload["with"].get("include-hidden-files") is hidden
    assert upload["with"]["if-no-files-found"] == "error"
    assert steps.index(mint) < steps.index(find) < steps.index(download) < steps.index(upload)
    assert job["outputs"]["queued"] == "${{ steps.republished.outputs.queued }}"


def test_every_unit_shard_downloads_the_one_republished_dev_durations():
    jobs = _jobs()
    steps = jobs["unit"]["steps"]
    download = _step(steps, "Download the queued run's dev durations")
    adopt = _step(steps, "Adopt latest dev durations")
    assert download["if"] == "needs.durations.outputs.queued == 'true'"
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {"name": "dev-durations", "path": "~/dev-durations"}
    assert steps.index(download) < steps.index(adopt)


@pytest.fixture
def lookup(tmp_path):
    find = _step(_jobs()["durations"]["steps"], "Find the latest passed dev push run")
    tools = tmp_path / "bin"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$ARGS"\n'
        'if [[ "$2" == */artifacts* ]]; then printf "%s" "$FAKE_KEPT"; else printf "%s" "$FAKE_RUN"; fi\n'
    )
    gh.chmod(0o755)

    def run(found, kept="sonar-report durations-merged coverage-baseline"):
        output = tmp_path / "output"
        output.write_text("")
        env = dict(
            os.environ,
            PATH=f"{tools}:{os.environ['PATH']}",
            ARGS=str(tmp_path / "args"),
            FAKE_RUN=found,
            FAKE_KEPT=kept,
            GITHUB_OUTPUT=str(output),
            GITHUB_REPOSITORY="the-cloud-clockwork/agentihooks",
        )
        result = subprocess.run(["bash", "-e", "-c", find["run"]], env=env, capture_output=True, text=True)
        return result, output.read_text(), (tmp_path / "args").read_text().splitlines()

    return run


def test_the_lookup_asks_for_successful_dev_push_runs_of_the_tests_workflow(lookup):
    result, output, args = lookup("37847322607")
    assert result.returncode == 0, result.stderr
    assert output == "id=37847322607\nbaseline=true\n"
    assert "37847322607" in result.stdout
    assert args[:2] == [
        "api",
        "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs"
        "?branch=dev&event=push&status=success&per_page=1",
    ]
    assert "repos/the-cloud-clockwork/agentihooks/actions/runs/37847322607/artifacts?per_page=100" in args
    assert '[.artifacts[] | select(.expired | not) | .name] | join(" ")' in args


def test_a_dev_run_without_a_baseline_restores_durations_only(lookup):
    result, output, _ = lookup("37847322607", kept="durations-merged coverage-baseline-old")
    assert result.returncode == 0, result.stderr
    assert output == "id=37847322607\nbaseline=false\n"


def test_the_lookup_is_red_when_the_dev_run_kept_no_live_durations(lookup):
    result, output, _ = lookup("37847322607", kept="coverage-baseline durations-merged-old")
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert output == ""


def test_the_lookup_is_red_when_no_dev_push_run_passed(lookup):
    result, output, _ = lookup("")
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert output == ""
