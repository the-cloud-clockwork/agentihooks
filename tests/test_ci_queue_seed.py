import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

_QUEUE = "github.event_name == 'merge_group'"


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
    assert not _runs_on(lookup, "merge_group")
    assert _runs_on(mint, "merge_group")
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"] == {
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-actions": "read",
        "permission-contents": "read",
    }
    assert job["permissions"] == {"contents": "read"}


def test_queue_runs_restore_the_latest_dev_durations_on_the_app_token():
    job = _jobs()["durations"]
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


def test_no_queue_run_carries_a_baseline_from_the_latest_dev_run():
    jobs = _jobs()
    baselines = [s for s in jobs["durations"]["steps"] if "baseline" in s.get("name", "")]
    assert baselines and not [s for s in baselines if _runs_on(s, "merge_group")]
    assert "dev-coverage-baseline" not in (_ROOT / ".github/workflows/test.yml").read_text()


def test_queue_runs_publish_their_own_baseline_before_sonar_and_the_gate():
    jobs = _jobs()
    steps = jobs["queue-baseline"]["steps"]
    download = _step(steps, "Download this run's shard coverage")
    record = _step(steps, "Record this run's coverage baseline")
    upload = _step(steps, "Publish this run's coverage baseline")
    find = _step(steps, "Find the run that measured the base tree")
    assert jobs["queue-baseline"]["needs"] == ["unit"]
    assert download["with"] == {"pattern": "coverage-3.12-*", "path": ".coverage-shards"}
    assert record["run"] == (
        "python -m tests.coverage_baseline --shards 8 --head-shards .coverage-shards"
        ' --out "$RUNNER_TEMP/own-baseline/baseline.json"'
    )
    assert upload["with"]["name"] == "coverage-baseline"
    assert upload["with"]["path"] == "${{ runner.temp }}/own-baseline/"
    assert steps.index(download) < steps.index(record) < steps.index(upload) < steps.index(find)
    assert "github.event_name == 'push'" in jobs["coverage-baseline"]["if"]
    assert "merge_group" not in jobs["coverage-baseline"]["if"]


def test_the_queue_baseline_holds_the_exact_base_tree_on_the_app_token():
    job = _jobs()["queue-baseline"]
    steps = job["steps"]
    mint = _step(steps, "Mint the tcc main ci App token")
    find = _step(steps, "Find the run that measured the base tree")
    download = _step(steps, "Download the base tree's coverage baseline")
    hold = _step(steps, "Hold the base tree")
    upload = _step(steps, "Publish the base tree's coverage baseline")
    assert job["needs"] == ["unit"]
    assert job["if"] == _QUEUE
    assert job["permissions"] == {"contents": "read"}
    assert mint["id"] == "app-token"
    assert mint["with"]["permission-actions"] == mint["with"]["permission-contents"] == "read"
    assert find["env"] == {
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
        "BASE_SHA": "${{ github.event.merge_group.base_sha }}",
    }
    assert download["with"] == {
        "name": "coverage-baseline",
        "path": "~/coverage-baseline",
        "run-id": "${{ steps.base-run.outputs.id }}",
        "repository": "${{ github.repository }}",
        "github-token": "${{ steps.app-token.outputs.token }}",
    }
    assert hold["env"] == {"BASE_TREE": "${{ steps.base-run.outputs.tree }}"}
    assert upload["with"]["name"] == "queue-coverage-baseline"
    assert upload["with"]["path"] == "~/coverage-baseline/"
    assert upload["with"]["if-no-files-found"] == "error"
    assert [steps.index(s) for s in (mint, find, download, hold, upload)] == sorted(
        steps.index(s) for s in (mint, find, download, hold, upload)
    )
    assert 'wait="${WAIT_SECONDS:-600}"' in find["run"]
    assert job["timeout-minutes"] * 60 > 600


@pytest.mark.parametrize(("measured", "fails"), [("abc", False), ("old", True)])
def test_a_restored_baseline_for_another_tree_is_red(tmp_path, measured, fails):
    hold = _step(_jobs()["queue-baseline"]["steps"], "Hold the base tree")
    folder = tmp_path / "coverage-baseline"
    folder.mkdir()
    (folder / "baseline.json").write_text(f'{{"tree": "{measured}"}}')
    env = dict(os.environ, HOME=str(tmp_path), BASE_TREE="abc")
    result = subprocess.run(["bash", "-e", "-c", hold["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode != 0) is fails
    assert ("::error::" in result.stdout) is fails


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


@pytest.fixture
def base_run(tmp_path):
    find = _step(_jobs()["queue-baseline"]["steps"], "Find the run that measured the base tree")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" >> "$ARGS"\n'
        'case "$2" in\n'
        '  */commits/*) printf "tree-of-base" ;;\n'
        '  */runs\\?head_sha=*) [[ -z "$FAIL_RUNS" ]] || exit 1; printf "%s\\n" $FAKE_RUNS ;;\n'
        '  */artifacts*) n=$(cat "$POLLS"); echo $((n + 1)) > "$POLLS"; '
        '[[ "$2" == *"/$FAKE_KEPT_BY/"* && $n -ge $FAKE_AFTER ]] && printf 1 || printf 0 ;;\n'
        "esac\n"
    )
    (tools / "gh").chmod(0o755)
    (tools / "sleep").write_text("#!/usr/bin/env bash\ntrue\n")
    (tools / "sleep").chmod(0o755)

    def run(runs="111 222", kept_by="222", after=0, wait="600", fail_runs=""):
        output = tmp_path / "output"
        output.write_text("")
        polls = tmp_path / "polls"
        polls.write_text("0")
        env = dict(
            os.environ,
            PATH=f"{tools}:{os.environ['PATH']}",
            ARGS=str(tmp_path / "args"),
            POLLS=str(polls),
            FAKE_RUNS=runs,
            FAKE_KEPT_BY=kept_by,
            FAKE_AFTER=str(after),
            FAIL_RUNS=fail_runs,
            WAIT_SECONDS=wait,
            BASE_SHA="b" * 40,
            GITHUB_OUTPUT=str(output),
            GITHUB_REPOSITORY="the-cloud-clockwork/agentihooks",
        )
        result = subprocess.run(["bash", "-e", "-c", find["run"]], env=env, capture_output=True, text=True)
        return result, output.read_text(), (tmp_path / "args").read_text().splitlines()

    return run


def test_the_base_run_is_any_tests_run_on_the_base_commit_that_kept_a_baseline(base_run):
    result, output, args = base_run()
    assert result.returncode == 0, result.stderr
    assert output == "id=222\ntree=tree-of-base\n"
    assert "repos/the-cloud-clockwork/agentihooks/commits/" + "b" * 40 in args
    assert (
        "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs?head_sha=" + "b" * 40 + "&per_page=20"
    ) in args
    assert "repos/the-cloud-clockwork/agentihooks/actions/runs/222/artifacts?name=coverage-baseline" in args
    assert "[.artifacts[] | select(.expired | not)] | length" in args
    assert "tree-of-base" in result.stdout


def test_the_base_run_waits_for_an_earlier_queue_entry_to_publish(base_run):
    result, output, _ = base_run(after=5)
    assert result.returncode == 0, result.stderr
    assert output == "id=222\ntree=tree-of-base\n"


def test_a_failed_runs_listing_is_red_at_once_instead_of_waiting(base_run):
    result, output, args = base_run(fail_runs="1")
    assert result.returncode != 0
    assert output == ""
    assert not [arg for arg in args if "/artifacts" in arg]


@pytest.mark.parametrize("runs", ["111 222", ""])
def test_the_base_run_is_red_once_the_bounded_wait_ends(base_run, runs):
    result, output, _ = base_run(runs=runs, kept_by="333", wait="0")
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert output == ""


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
    condition = step.get("if", "true").removeprefix("${{").removesuffix("}}").strip()
    if condition == "true":
        return True
    for clause in condition.split("||"):
        held = True
        for atom in clause.split("&&"):
            name, op, value = atom.strip().strip("()").split()
            assert name == "github.event_name", atom
            held = held and ((value.strip("'") == event) == (op == "=="))
        if held:
            return True
    return False


def test_dispatch_runs_restore_durations_and_the_base_baseline_on_the_app_token():
    steps = _jobs()["durations"]["steps"]
    restored = [s for s in steps if _runs_on(s, "workflow_dispatch")]
    names = [s.get("name") for s in restored]
    assert "Look up the base revision's dev durations" not in names
    assert names == [
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


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_pull_request_and_push_runs_keep_the_dev_cache_lookup(event):
    steps = _jobs()["durations"]["steps"]
    assert [s.get("name") for s in steps if _runs_on(s, event)] == ["Look up the base revision's dev durations"]


@pytest.fixture
def dispatch_lookup(tmp_path):
    find = _step(_jobs()["durations"]["steps"], "Find the dev push run of the dispatched base")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" >> "$ARGS"\n'
        'case "$2" in\n'
        '  */commits/*) [[ -z "$FAIL_COMMIT" ]] || exit 1; printf "%s" "$FAKE_SHA" ;;\n'
        '  */runs\\?*) printf "%s\\n" $FAKE_RUNS ;;\n'
        '  */runs/*/artifacts*) run="${2#*/runs/}"; run="${run%%/*}"; v="KEPT_$run"; printf "%s" "${!v}" ;;\n'
        "esac\n"
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
            FAIL_COMMIT=fail_commit,
            GITHUB_OUTPUT=str(output),
            GITHUB_REPOSITORY="the-cloud-clockwork/agentihooks",
            **{f"KEPT_{k.removeprefix('run')}": v for k, v in kept.items()},
        )
        result = subprocess.run(["bash", "-e", "-c", find["run"]], env=env, capture_output=True, text=True)
        args = (tmp_path / "args").read_text().splitlines() if (tmp_path / "args").exists() else []
        return result, output.read_text(), args

    return run


def test_dispatch_restores_from_the_dev_push_run_on_the_pinned_base(dispatch_lookup):
    result, output, args = dispatch_lookup(
        run111="durations-merged", run222="sonar-report durations-merged coverage-baseline"
    )
    assert result.returncode == 0, result.stderr
    assert output == "id=222\n"
    assert "repos/the-cloud-clockwork/agentihooks/commits/dev" in args
    assert (
        "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs"
        "?branch=dev&event=push&head_sha=" + "c" * 40 + "&per_page=20"
    ) in args
    assert "c" * 40 in result.stdout


def test_dispatch_resolves_a_pinned_commit_as_given(dispatch_lookup):
    result, _, args = dispatch_lookup(base="d" * 40, run111="durations-merged coverage-baseline")
    assert result.returncode == 0, result.stderr
    assert "repos/the-cloud-clockwork/agentihooks/commits/" + "d" * 40 in args


@pytest.mark.parametrize(
    ("runs", "kept"),
    [
        ("", {}),
        ("111", {"run111": "durations-merged"}),
        ("111", {"run111": "coverage-baseline durations-merged-old"}),
    ],
)
def test_dispatch_is_red_when_no_dev_push_run_on_the_base_kept_both(dispatch_lookup, runs, kept):
    result, output, _ = dispatch_lookup(runs=runs, **kept)
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "origin/dev" in result.stdout
    assert output == ""


def test_dispatch_is_red_when_the_base_does_not_resolve(dispatch_lookup):
    result, output, args = dispatch_lookup(fail_commit="1")
    assert result.returncode != 0
    assert output == ""
    assert not [arg for arg in args if "/runs" in arg]
