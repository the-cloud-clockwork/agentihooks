import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ci_anton_runners import event as github_event
from tests.test_ci_anton_runners import resolve

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

_QUEUE = "github.event_name == 'merge_group'"


def _jobs():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _duration_steps():
    jobs = _jobs()
    steps = jobs.get("durations", jobs["select"])["steps"]
    end = next(index for index, step in enumerate(steps) if step.get("id") == "republished")
    return [step for step in steps[: end + 1] if not step.get("name", "").endswith("attempt 2")]


def _step(steps, name):
    return next(s for s in steps if s.get("name") == name)


def test_publisher_uploads_the_passed_dev_baseline_for_queue_runs():
    job = _jobs()["coverage-baseline"]
    save = _step(job["steps"], "Save the passed dev coverage baseline")
    upload = _step(job["steps"], "Publish the passed dev coverage baseline")
    assert upload["uses"] == "./.github/actions/retry-upload"
    assert upload["with"]["name"] == "coverage-baseline"
    assert upload["with"]["path"] == save["with"]["path"] == "~/coverage-baseline"
    assert upload["with"]["if-no-files-found"] == "error"


def test_queue_runs_mint_a_read_only_app_token_and_skip_the_dev_cache_lookup():
    job = _jobs().get("durations", _jobs()["select"])
    lookup = _step(job["steps"], "Look up the base revision's dev durations")
    mint = _step(job["steps"], "Mint the tcc main ci App token")
    assert not _runs_on(lookup, "merge_group")
    assert _runs_on(mint, "merge_group")
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"] == {
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-actions": "${{ github.event_name == 'merge_group' && 'write' || 'read' }}",
        "permission-contents": "read",
    }
    assert job["permissions"] == {"contents": "read", "actions": "read"}


def test_queue_runs_restore_the_latest_dev_durations_on_the_app_token():
    job = _jobs().get("durations", _jobs()["select"])
    steps = job["steps"]
    mint = _step(steps, "Mint the tcc main ci App token")
    find = _step(steps, "Find the latest passed dev push run")
    download = _step(steps, "Download the dev durations")
    upload = _step(steps, "Republish the dev durations")
    assert find["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert mint["id"] == "app-token"
    assert find["if"] == _QUEUE
    assert _runs_on(download, "merge_group") and _runs_on(upload, "merge_group")
    assert download["with"] == {
        "name": "durations-merged",
        "path": "~/dev-durations",
        "run-id": "${{ steps.dev-run.outputs.id || steps.base-run.outputs.id }}",
        "repository": "${{ github.repository }}",
        "github-token": "${{ steps.app-token.outputs.token }}",
    }
    assert upload["with"]["name"] == "dev-durations"
    assert upload["with"]["path"] == "~/dev-durations/"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"
    assert steps.index(mint) < steps.index(find) < steps.index(download) < steps.index(upload)
    assert job["outputs"]["queued"] == "${{ steps.republished.outputs.queued }}"
    assert job["outputs"]["base"] == "${{ steps.base-run.outputs.sha }}"


def test_no_queue_run_carries_a_baseline_from_the_latest_dev_run():
    jobs = _jobs()
    baselines = [
        s
        for s in jobs.get("durations", jobs["select"])["steps"]
        if "baseline" in s.get("name", "") and not s["name"].endswith("attempt 2")
    ]
    assert baselines and not [s for s in baselines if _runs_on(s, "merge_group")]
    assert "dev-coverage-baseline" not in (_ROOT / ".github/workflows/test.yml").read_text()


def test_queue_has_no_coverage_baseline_publisher():
    jobs = _jobs()
    assert "queue-baseline" not in jobs
    assert "github.event_name == 'push'" in jobs["coverage-baseline"]["if"]
    assert "merge_group" not in jobs["coverage-baseline"]["if"]


@pytest.mark.parametrize(
    ("workflow", "name"),
    [
        (path.name, name)
        for path in sorted((_ROOT / ".github/workflows").glob("*.yml"))
        for name in (yaml.safe_load(path.read_text()).get("jobs") or {})
    ],
)
def test_no_job_waits_on_the_retired_queue_baseline(workflow, name):
    jobs = yaml.safe_load((_ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    assert "queue-baseline" not in jobs
    assert "queue-baseline" not in jobs[name].get("needs", [])


@pytest.mark.parametrize(
    "step,key",
    [
        ("Restore measured coverage history", "coverage-baseline-\u0024{{ steps.tree.outputs.previous }}"),
        ("Save the passed dev coverage baseline", "coverage-baseline-\u0024{{ steps.tree.outputs.tree }}"),
    ],
)
def test_push_baselines_keep_the_measured_tree_key(step, key):
    entry = _step(_jobs()["coverage-baseline"]["steps"], step)
    assert entry["with"]["key"] == key


def test_push_history_falls_back_to_the_newest_dev_baseline_when_the_previous_run_was_superseded():
    entry = _step(_jobs()["coverage-baseline"]["steps"], "Restore measured coverage history")
    assert entry["with"]["restore-keys"] == "coverage-baseline-"


def test_every_unit_shard_downloads_the_one_republished_dev_durations():
    jobs = _jobs()
    steps = jobs["split"]["steps"]
    download = _step(steps, "Download the queued run's dev durations")
    adopt = _step(steps, "Adopt latest dev durations")
    producer = "durations" if "durations" in jobs else "select"
    assert download["if"] == f"needs.{producer}.outputs.source == '' && needs.{producer}.outputs.queued == 'true'"
    assert download["uses"] == "./.github/actions/retry-download"
    assert download["with"] == {
        "name": "dev-durations",
        "path": "~/dev-durations",
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
    }
    assert steps.index(download) < steps.index(adopt)


@pytest.fixture
def lookup(tmp_path):
    find = _step(_jobs().get("durations", _jobs()["select"])["steps"], "Find the latest passed dev push run")
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
    assert output == "id=37847322607\n"
    assert "37847322607" in result.stdout
    assert args[:2] == [
        "api",
        "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs"
        "?branch=dev&event=push&status=success&per_page=1",
    ]
    assert "repos/the-cloud-clockwork/agentihooks/actions/runs/37847322607/artifacts?per_page=100" in args
    assert '[.artifacts[] | select(.expired | not) | .name] | join(" ")' in args


def test_a_dev_run_without_a_baseline_still_restores_its_durations(lookup):
    result, output, _ = lookup("37847322607", kept="durations-merged")
    assert result.returncode == 0, result.stderr
    assert output == "id=37847322607\n"


@pytest.mark.parametrize(
    "name",
    [
        "worker-image",
        "swarm-image",
        "brain-smoke",
        "writer-lock",
        "install-smoke",
        "ledger-load",
        "kind-due",
        "coverage-ratchet",
        "stage-budget",
    ],
)
def test_queue_excludes_promotion_jobs(name):
    from tests.contracts.ci_tiers.test_workflow import jobs_for

    assert name not in jobs_for("merge_group")


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_push_and_dispatch_keep_the_coverage_ratchet_and_stage_report(event):
    from tests.contracts.ci_tiers.test_workflow import jobs_for

    assert {"coverage-ratchet", "stage-budget"} <= jobs_for(event)


def test_failed_units_cannot_publish_a_push_baseline():
    job = _jobs()["coverage-baseline"]
    assert "needs.unit.result == 'success'" in job["if"]
    assert job["needs"] in (["unit", "gate-required"], ["unit", "coverage-ratchet"])


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


def _runs_on(step, event):
    condition = str(step.get("if", "'true'")).removeprefix("${{").removesuffix("}}")
    condition = condition.replace("steps.provenance.outputs.recorded", "'true'")
    condition = re.sub(
        r"hashFiles\('([^']+)'\)", lambda found: "'x'" if (_ROOT / found.group(1)).exists() else "''", condition
    )
    return bool(resolve("${{ " + condition + " }}", **github_event(event, actor="github-merge-queue[bot]")))


def test_dispatch_runs_restore_durations_and_the_base_baseline_on_the_app_token():
    steps = _duration_steps()
    restored = [s for s in steps if _runs_on(s, "workflow_dispatch")]
    names = [s.get("name") for s in restored]
    assert "Look up the base revision's dev durations" not in names
    assert names == [
        "Turn off automatic git maintenance",
        "Mint the tcc main ci App token",
        "Find the dev push run of the dispatched base",
        "Download the dev durations",
        "Download the dispatched base's coverage baseline",
        "Republish the dev durations",
        "Republish the dispatched base's coverage baseline",
        "Mark the dev artifacts restored",
    ]
    find = _step(steps, "Find the dev push run of the dispatched base")
    assert find["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}", "BASE": "${{ inputs.base }}"}
    assert _step(steps, "Download the dev durations")["with"]["run-id"] == (
        "${{ steps.dev-run.outputs.id || steps.base-run.outputs.id }}"
    )
    assert _step(steps, "Download the dispatched base's coverage baseline")["with"] == {
        "name": "coverage-baseline",
        "path": "~/coverage-baseline",
        "run-id": "${{ steps.base-run.outputs.id }}",
        "repository": "${{ github.repository }}",
        "github-token": "${{ steps.app-token.outputs.token }}",
    }
    upload = _step(steps, "Republish the dispatched base's coverage baseline")
    assert upload["with"]["name"] == "queue-coverage-baseline"
    assert upload["with"]["path"] == "~/coverage-baseline/"
    assert upload["with"]["if-no-files-found"] == "error"


_REUSE = [
    "Install unit reuse metadata dependencies",
    "Look up the base revision's dev durations",
    "Mint the tcc main ci App token",
    "Fetch passed queue unit evidence for this dev commit",
    "Select complete unit results from the same queue commit",
]


@pytest.mark.parametrize(
    "event, expected",
    [
        (
            "pull_request",
            [
                None,
                None,
                "Install provenance dependencies",
                "Record the protected tested tree",
                "Publish protected tested tree evidence",
                "Turn off automatic git maintenance",
                "Look up the base revision's dev durations",
                "Mint the tcc main ci App token",
            ],
        ),
        ("push", ["Turn off automatic git maintenance", None, None, *_REUSE]),
    ],
)
def test_pull_request_and_push_runs_keep_the_dev_cache_lookup(event, expected):
    steps = _duration_steps()
    assert [s.get("name") for s in steps if _runs_on(s, event)] == expected


_RUN_OF = 'split(":") as $p | {id: ($p[0] | tonumber), head_sha: ($p[1] // $sha), conclusion: ($p[2] // "success")}'
_LISTED = (
    '{workflow_runs: map(select(($q | contains("&status=success&") | not) or .conclusion == "success")'
    ' | select(("&head_sha=" + .head_sha + "&") as $h | ($q | contains("&head_sha=") | not) or ($q | contains($h))))}'
)


@pytest.fixture
def dispatch_lookup(tmp_path):
    find = _step(_jobs().get("durations", _jobs()["select"])["steps"], "Find the dev push run of the dispatched base")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" >> "$ARGS"\n'
        'case "$2" in\n'
        '  */compare/*) body=\'{"status":"ahead"}\' ;;\n'
        '  */jobs*) jq -nc \'[range(1;13) | {name: ("unit (3.12, " + tostring + ")"), conclusion: "success"}] + [range(1;7) | {name: ("unit (3.11, " + tostring + ")"), conclusion: "success"}] + [{name: "coverage-baseline", conclusion: "success", steps: [{name: "Compute the coverage baseline", conclusion: "success"}, {name: "Publish the passed dev coverage baseline", conclusion: "success"}]}]\' | jq -c \'.[]\'; exit 0 ;;\n'
        '  */commits/*) [[ -z "$FAIL_COMMIT" ]] || exit 1; body="{\\"sha\\": \\"$FAKE_SHA\\"}" ;;\n'
        '  */runs\\?*) body=$(printf "%s\\n" $FAKE_RUNS | grep . | jq -R --arg sha "$FAKE_SHA" "$RUN_OF"'
        ' | jq -s --arg q "&${2#*\\?}&" "$LISTED" | jq --arg q "&${2#*\\?}&" \'.workflow_runs |= map(. + {event: "push", head_branch: "dev"} | select(($q | contains("&status=completed&") | not) or (.conclusion != "" and .conclusion != null)))\') ;;\n'
        '  */runs/*/artifacts*) run="${2#*/runs/}"; run="${run%%/*}"; v="KEPT_$run"; body="${!v:-[]}"'
        '; body="{\\"artifacts\\": $body}" ;;\n'
        "esac\n"
        'jq -r "$4" <<< "$body"\n'
    )
    (tools / "gh").chmod(0o755)

    def run(base="origin/dev", sha="c" * 40, runs="111 222", fail_commit="", **kept):
        output = tmp_path / "output"
        output.write_text("")
        env = dict(
            os.environ,
            PATH=f"{tools}:{os.environ['PATH']}",
            ARGS=str(tmp_path / "args"),
            BASE=base,
            FAKE_SHA=sha,
            FAKE_RUNS=runs,
            RUN_OF=_RUN_OF,
            LISTED=_LISTED,
            FAIL_COMMIT=fail_commit,
            GITHUB_OUTPUT=str(output),
            GITHUB_REF_NAME="feature",
            GITHUB_REPOSITORY="the-cloud-clockwork/agentihooks",
            **{
                f"KEPT_{k.removeprefix('run')}": json.dumps(
                    [{"name": n.lstrip("~"), "expired": n.startswith("~")} for n in v.split()]
                )
                for k, v in kept.items()
            },
        )
        result = subprocess.run(["bash", "-e", "-c", find["run"]], env=env, capture_output=True, text=True)
        args = (tmp_path / "args").read_text().splitlines() if (tmp_path / "args").exists() else []
        return result, output.read_text(), args

    return run


def test_dispatch_on_dev_restores_the_newest_passed_dev_push_run(dispatch_lookup):
    result, output, args = dispatch_lookup(
        runs=f"333:{'c' * 40}: 222:{'b' * 40}:success 111:{'a' * 40}:success",
        run333="durations-merged coverage-baseline",
        run222="sonar-report durations-merged coverage-baseline",
        run111="durations-merged coverage-baseline",
    )
    assert result.returncode == 0, result.stderr
    assert output == f"id=222\nsha={'b' * 40}\n"
    assert not [arg for arg in args if "/commits/" in arg]
    assert (
        "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs"
        "?branch=dev&event=push&status=completed&per_page=20"
    ) in args
    assert "from measured dev push run 222" in result.stdout


def test_dispatch_on_a_pinned_commit_restores_its_passed_dev_push_run(dispatch_lookup):
    pinned = "d" * 40
    result, output, args = dispatch_lookup(
        base=pinned,
        sha=pinned,
        runs=f"444:{pinned}: 222:{'b' * 40}:success 111:{pinned}:success",
        run444="durations-merged coverage-baseline",
        run111="durations-merged coverage-baseline",
        run222="durations-merged coverage-baseline",
    )
    assert result.returncode == 0, result.stderr
    assert output == f"id=111\nsha={pinned}\n"
    assert "repos/the-cloud-clockwork/agentihooks/commits/" + pinned in args
    assert (
        "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs"
        "?branch=dev&event=push&status=completed&head_sha=" + pinned + "&per_page=20"
    ) in args


@pytest.mark.parametrize("base", ["origin/dev", "d" * 40])
def test_dispatch_is_red_when_only_an_unfinished_dev_push_run_kept_both(dispatch_lookup, base):
    result, output, _ = dispatch_lookup(
        base=base, sha="d" * 40, runs=f"333:{'d' * 40}:", run333="durations-merged coverage-baseline"
    )
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert output == ""


@pytest.mark.parametrize(
    ("runs", "kept"),
    [
        ("", {}),
        ("111", {"run111": "durations-merged"}),
        ("111", {"run111": "coverage-baseline durations-merged-old"}),
        ("111", {"run111": "~durations-merged ~coverage-baseline"}),
    ],
)
def test_dispatch_is_red_when_no_dev_push_run_on_the_base_kept_both(dispatch_lookup, runs, kept):
    result, output, _ = dispatch_lookup(runs=runs, **kept)
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "origin/dev" in result.stdout
    assert output == ""


def test_dispatch_is_red_when_the_base_does_not_resolve(dispatch_lookup):
    result, output, args = dispatch_lookup(base="d" * 40, fail_commit="1")
    assert result.returncode != 0
    assert output == ""
    assert not [arg for arg in args if "/runs" in arg]
