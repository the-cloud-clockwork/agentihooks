import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_RETRY_UPLOAD = "./.github/actions/retry-upload"
_TRANSFER = ("actions/upload-artifact@", "actions/download-artifact@")
_TRUE_ATOMS = (
    "inputs.after-cancel == 'true' || !cancelled()",
    "steps.files.outputs.found == 'true'",
    "needs.plan.outputs.changed != '0'",
    "always()",
)


def _jobs():
    return yaml.safe_load((_ROOT / ".github/workflows/mutation-preflight.yml").read_text())["jobs"]


def _upload_steps():
    return yaml.safe_load((_ROOT / ".github/actions/retry-upload/action.yml").read_text())["runs"]["steps"]


def _finalization(job):
    steps = _jobs()[job]["steps"]
    if job == "mutation-shard":
        names = [step.get("name") for step in steps]
        return steps[names.index("Mutate changed Python files") + 1 :]
    return steps


def _runs(condition, outcomes):
    if condition is None:
        return True
    resolved = re.sub(
        r"steps\.([\w-]+)\.outcome == 'failure'", lambda m: str(outcomes.get(m[1]) == "failure"), condition
    )
    for atom in _TRUE_ATOMS:
        resolved = resolved.replace(atom, "True")
    terms = [term.strip(" ()") for term in resolved.split("&&")]
    assert set(terms) <= {"True", "False"}, condition
    return all(term == "True" for term in terms)


def _run(steps, budget):
    outcomes, failed, attempts = {}, False, 0
    for step in steps:
        if not _runs(step.get("if"), outcomes):
            continue
        uses = step.get("uses", "")
        if uses == _RETRY_UPLOAD:
            nested_failed, budget, nested_attempts = _run(_upload_steps(), budget)
            attempts += nested_attempts
            outcome = "failure" if nested_failed else "success"
        elif uses.startswith(_TRANSFER):
            attempts += 1
            outcome = "failure" if budget else "success"
            budget -= bool(budget)
        else:
            outcome = "success"
        if "id" in step:
            outcomes[step["id"]] = outcome
        if outcome == "failure" and step.get("continue-on-error") is not True:
            failed = True
            break
    return failed, budget, attempts


def _transfers(job):
    for step in _finalization(job):
        uses = step.get("uses", "")
        if uses == _RETRY_UPLOAD or (uses.startswith(_TRANSFER) and not step["name"].endswith(", attempt 2")):
            yield step


def _groups(job):
    steps = _finalization(job)
    names = [step.get("name") for step in steps]
    for transfer in _transfers(job):
        start = steps.index(transfer)
        retried = f"{transfer['name']}, attempt 2"
        end = names.index(retried) + 1 if retried in names else start + 1
        yield transfer["name"], steps[start:end]


def _attempts(group):
    return 3 if group[0].get("uses") == _RETRY_UPLOAD else 2


@pytest.mark.parametrize(
    "job, uploads, downloads",
    [
        ("mutation-shard", ["Upload the tests that killed mutants", "Upload shard mutation evidence"], []),
        (
            "mutation",
            ["Upload complete mutation evidence"],
            ["Download every mutation shard report", "Download the tests that killed mutants"],
        ),
    ],
)
def test_mutation_finalization_transfers_are_all_retried(job, uploads, downloads):
    names = {step["name"]: step for step in _transfers(job)}
    assert set(names) == {*uploads, *downloads}
    for name in uploads:
        assert names[name]["uses"] == _RETRY_UPLOAD
        assert names[name]["with"]["after-cancel"] is True
    for name in downloads:
        assert names[name]["uses"].startswith("actions/download-artifact@")
        assert names[name]["continue-on-error"] is True


@pytest.mark.parametrize(
    "job, name, given",
    [
        (
            "mutation-shard",
            "Upload the tests that killed mutants",
            {
                "after-cancel": True,
                "name": "mutation-killers-${{ matrix.shard }}",
                "path": ".mutation-killers/",
                "include-hidden-files": True,
                "if-no-files-found": "ignore",
                "retention-days": 1,
            },
        ),
        (
            "mutation-shard",
            "Upload shard mutation evidence",
            {
                "after-cancel": True,
                "name": "mutation-preflight-shard-${{ matrix.shard }}",
                "path": "${{ job.status == 'success' && '.mutation-gate/report.json' || '.mutation-gate/' }}",
                "include-hidden-files": True,
                "retention-days": 7,
            },
        ),
        (
            "mutation",
            "Upload complete mutation evidence",
            {
                "after-cancel": True,
                "name": "mutation-preflight-report",
                "path": ".mutation-gate/",
                "include-hidden-files": True,
                "retention-days": 7,
            },
        ),
    ],
)
def test_the_retried_uploads_keep_their_names_paths_and_retention(job, name, given):
    step = next(s for s in _jobs()[job]["steps"] if s.get("name") == name)
    assert step["if"] == "always()"
    assert step["with"] == given


def test_the_aggregator_outlasts_a_report_upload_that_uses_every_attempt():
    action = yaml.safe_load((_ROOT / ".github/actions/retry-upload/action.yml").read_text())
    worst = 3 * (int(action["inputs"]["attempt-seconds"]["default"]) + 10) + 10 + 20
    assert _jobs()["mutation"]["timeout-minutes"] * 60 > worst + 2 * 30


@pytest.mark.parametrize("job", ["mutation-shard", "mutation"])
def test_one_planted_reset_on_each_finalization_transfer_no_longer_fails_the_job(job):
    for name, group in _groups(job):
        assert _run(group, budget=1) == (False, 0, 2), f"{name}: one connection reset failed the job"


@pytest.mark.parametrize("job", ["mutation-shard", "mutation"])
def test_a_reset_that_outlasts_every_attempt_still_fails_the_job(job):
    for name, group in _groups(job):
        attempts = _attempts(group)
        assert _run(group, budget=attempts - 1) == (False, 0, attempts), name
        assert _run(group, budget=attempts) == (True, 0, attempts), f"{name}: retries are unbounded"


@pytest.mark.parametrize("name", ["Download every mutation shard report", "Download the tests that killed mutants"])
def test_the_aggregator_downloads_wait_and_name_the_outage_before_attempt_two(name, tmp_path):
    steps = _jobs()["mutation"]["steps"]
    first = next(s for s in steps if s.get("name") == name)
    index = steps.index(first)
    wait, second = steps[index + 1 : index + 3]
    retry = f"steps.{first['id']}.outcome == 'failure'"
    assert wait["if"] == second["if"] == retry
    assert wait["name"] == f"{name}, wait before attempt 2"
    assert second["name"] == f"{name}, attempt 2"
    assert (second["uses"], second["with"]) == (first["uses"], first["with"])
    assert "continue-on-error" not in second
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "sleep").write_text('#!/usr/bin/env bash\necho "$1" >> "$SLEPT"\n')
    (binary / "sleep").chmod(0o755)
    done = subprocess.run(
        [wait["shell"], "-e", "-c", wait["run"]],
        cwd=tmp_path,
        env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", SLEPT=str(tmp_path / "slept")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(f"::warning::{name} failed, as an artifact service outage does;")
    assert (tmp_path / "slept").read_text() == "30\n"


def test_a_partial_shard_report_download_is_cleared_before_attempt_two(tmp_path):
    steps = _jobs()["mutation"]["steps"]
    wait = next(s for s in steps if s.get("name") == "Download every mutation shard report, wait before attempt 2")
    partial = tmp_path / ".mutation-shards/mutation-preflight-shard-0/report.json"
    partial.parent.mkdir(parents=True)
    partial.write_text("{")
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "sleep").write_text("#!/usr/bin/env bash\n")
    (binary / "sleep").chmod(0o755)
    done = subprocess.run(
        [wait["shell"], "-e", "-c", wait["run"]],
        cwd=tmp_path,
        env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / ".mutation-shards").exists()


@pytest.mark.parametrize(
    "name, inputs",
    [
        ("Download planned mutation test selection", {"name": "mutation-selection"}),
        (
            "Download the shared mutation stats",
            {"pattern": "mutation-stats-*", "path": ".mutation-stats", "merge-multiple": True},
        ),
        ("Download the measured mutation plan", {"name": "mutation-plan", "path": ".mutation-stats"}),
    ],
)
@pytest.mark.parametrize(
    "resets, expected",
    [(0, (False, 0, 1)), (1, (False, 0, 2)), (2, (True, 0, 2))],
)
def test_shard_input_downloads_retry_one_reset_and_fail_persistent_resets(name, inputs, resets, expected):
    steps = _jobs()["mutation-shard"]["steps"]
    names = [step.get("name") for step in steps]
    start = names.index(name)
    retry_name = f"{name}, attempt 2"
    end = names.index(retry_name) + 1 if retry_name in names else start + 1
    group = steps[start:end]
    assert _run(group, budget=resets) == expected, name
    assert group[0]["with"] == inputs
    if len(group) > 1:
        assert group[-1]["with"] == inputs
        assert group[-1].get("continue-on-error", False) is False
        assert group[1]["if"] == group[-1]["if"] == f"steps.{group[0]['id']}.outcome == 'failure'"
        assert "sleep 30" in group[1]["run"]
        assert "::warning::" in group[1]["run"]
