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


def _run(steps, resets):
    outcomes, failed = {}, False
    for step in steps:
        if not _runs(step.get("if"), outcomes):
            continue
        uses = step.get("uses", "")
        if uses == _RETRY_UPLOAD:
            nested_failed, resets = _run(_upload_steps(), resets)
            outcome = "failure" if nested_failed else "success"
        elif uses.startswith(_TRANSFER) and resets:
            resets -= 1
            outcome = "failure"
        else:
            outcome = "success"
        if "id" in step:
            outcomes[step["id"]] = outcome
        if outcome == "failure" and step.get("continue-on-error") is not True:
            failed = True
            break
    return failed, resets


def _transfers(job):
    for step in _finalization(job):
        uses = step.get("uses", "")
        if uses == _RETRY_UPLOAD or (uses.startswith(_TRANSFER) and not step["name"].endswith(", attempt 2")):
            yield step


def _groups(job):
    steps = _finalization(job)
    for transfer in _transfers(job):
        start = steps.index(transfer)
        end = start + (1 if transfer.get("uses") == _RETRY_UPLOAD else 3)
        yield transfer["name"], steps[start:end]


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


@pytest.mark.parametrize("job", ["mutation-shard", "mutation"])
def test_one_planted_reset_on_each_finalization_transfer_no_longer_fails_the_job(job):
    for name, group in _groups(job):
        failed, left = _run(group, resets=1)
        assert not failed, f"{name}: one connection reset failed the job"
        assert left == 0, f"{name}: the planted reset never reached a transfer"


@pytest.mark.parametrize("job", ["mutation-shard", "mutation"])
def test_a_reset_that_outlasts_every_attempt_still_fails_the_job(job):
    for name, group in _groups(job):
        attempts = 3 if group[0].get("uses") == _RETRY_UPLOAD else 2
        assert _run(group, resets=attempts - 1) == (False, 0), name
        assert _run(group, resets=attempts)[0] is True, f"{name}: retries are unbounded"


def test_the_aggregator_downloads_wait_and_name_the_outage_before_attempt_two(tmp_path):
    steps = _jobs()["mutation"]["steps"]
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "sleep").write_text('#!/usr/bin/env bash\necho "$1" >> "$SLEPT"\n')
    (binary / "sleep").chmod(0o755)
    for first in (s for s in steps if s.get("uses", "").startswith("actions/download-artifact@")):
        if first["name"].endswith(", attempt 2"):
            continue
        index = steps.index(first)
        wait, second = steps[index + 1 : index + 3]
        retry = f"steps.{first['id']}.outcome == 'failure'"
        assert wait["if"] == second["if"] == retry
        assert wait["name"] == f"{first['name']}, wait before attempt 2"
        assert second["name"] == f"{first['name']}, attempt 2"
        assert (second["uses"], second["with"]) == (first["uses"], first["with"])
        assert "continue-on-error" not in second
        slept = tmp_path / f"slept-{first['id']}"
        done = subprocess.run(
            [wait["shell"], "-e", "-c", wait["run"]],
            env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", SLEPT=str(slept)),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.startswith(f"::warning::{first['name']} failed, as an artifact service outage does;")
        assert slept.read_text() == "30\n"
