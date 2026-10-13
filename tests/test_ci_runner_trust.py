import pytest

from scripts import ci_runner_trust

pytestmark = pytest.mark.unit

ACTORS = ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"]
GUARD = (
    "github.event_name != 'pull_request_target' && "
    "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
    "contains(fromJSON(needs.runner-trust.outputs.actors), github.actor) && "
    "contains(fromJSON(needs.runner-trust.outputs.actors), github.triggering_actor) && "
    "(github.actor != fromJSON(needs.runner-trust.outputs.queue_actor) || github.event_name == 'merge_group' || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/dev')) && "
    "(github.triggering_actor != fromJSON(needs.runner-trust.outputs.queue_actor) || github.event_name == 'merge_group' || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/dev'))"
)


def workflow(runner="anton", guard=GUARD):
    return {
        "on": {"pull_request": {}, "push": {}, "workflow_dispatch": {}},
        "jobs": {
            "runner-trust": {
                "uses": "the-cloud-clockwork/agentihooks/.github/workflows/runner-trust.yml@dev",
            },
            "build": {"runs-on": runner, "needs": ["runner-trust"], "if": guard, "steps": []},
        },
    }


def test_safe_hosted_and_guarded_self_hosted_jobs_pass():
    workflows = {"build.yml": workflow(), "hosted.yaml": {"jobs": {"test": {"runs-on": "ubuntu-latest"}}}}
    assert ci_runner_trust.check(workflows, ACTORS) == []


@pytest.mark.parametrize(
    "runner",
    [
        "anton",
        "anton-light",
        "anton-batch",
        "self-hosted",
        ["linux", "self-hosted"],
        {"group": "private", "labels": "linux"},
        "${{ inputs.runner }}",
    ],
)
@pytest.mark.parametrize(
    "removed",
    [
        "github.event_name != 'pull_request_target'",
        "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)",
        "contains(fromJSON(needs.runner-trust.outputs.actors), github.actor)",
        "contains(fromJSON(needs.runner-trust.outputs.actors), github.triggering_actor)",
    ],
)
def test_each_untrusted_admission_is_red(runner, removed):
    guard = GUARD.replace(removed + " && ", "").removesuffix(" && " + removed)
    problems = ci_runner_trust.check({"build.yml": workflow(runner, guard)}, ACTORS)
    assert any("build.yml/build" in problem and "runner trust" in problem for problem in problems)


@pytest.mark.parametrize(
    "guard",
    [
        GUARD + " || true",
        "false || " + GUARD,
        "'" + GUARD + "'",
        "contains('" + GUARD + "', github.actor)",
        "! (" + GUARD + ")",
    ],
)
def test_guard_text_cannot_be_used_to_bypass_admission(guard):
    assert ci_runner_trust.check({"build.yml": workflow(guard=guard)}, ACTORS)


@pytest.mark.parametrize("change", ["local", "head", "input", "missing", "no-need"])
def test_allowlist_must_come_from_protected_reusable_workflow(change):
    candidate = workflow()
    trust = candidate["jobs"]["runner-trust"]
    if change == "local":
        trust["uses"] = "./.github/workflows/runner-trust.yml"
    elif change == "head":
        trust["uses"] = trust["uses"].replace("@dev", "@head")
    elif change == "input":
        trust["with"] = {"actors": ACTORS + ["stranger"]}
    elif change == "missing":
        del candidate["jobs"]["runner-trust"]
    else:
        candidate["jobs"]["build"]["needs"] = []
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS)


def test_target_workflow_cannot_ever_allocate_a_private_runner():
    candidate = workflow()
    candidate["on"] = {"pull_request_target": {}}
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS)


def test_matrix_include_cannot_hide_a_private_runner():
    candidate = workflow("${{ matrix.runner }}", "true")
    candidate["jobs"]["build"]["strategy"] = {"matrix": {"runner": ["ubuntu-latest"], "include": [{"runner": "anton"}]}}
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS)


def test_base_allowlist_requires_exact_approved_identities():
    assert ci_runner_trust.validate_actors(ACTORS) == ACTORS
    with pytest.raises(ValueError, match="trusted actors"):
        ci_runner_trust.validate_actors(ACTORS + ["stranger"])


def test_actor_output_loads_the_protected_file(tmp_path, capsys):
    import json

    policy = tmp_path / ".github/workflows/trusted-actors.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps(ACTORS))
    assert ci_runner_trust.main(["--root", str(tmp_path), "--actors-output"]) == 0
    assert json.loads(capsys.readouterr().out.strip().removeprefix("actors=")) == ACTORS


def test_repository_workflows_are_graded():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert ci_runner_trust.main(["--root", str(root)]) == 0


def test_protected_loader_reads_only_dev_approvals():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/runner-trust.yml").read_text())
    job = workflow["jobs"]["actors"]
    assert job["runs-on"] == "ubuntu-latest"
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "dev"
    assert checkout["with"]["repository"] == "the-cloud-clockwork/agentihooks"
    assert any("trusted-actors.json" in step.get("run", "") for step in job["steps"])
    assert ci_runner_trust.check({"runner-trust.yml": workflow}, ACTORS) == []


def test_actor_loader_cannot_import_checkout_modules(tmp_path):
    import json
    import shlex
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/runner-trust.yml").read_text())
    command = workflow["jobs"]["actors"]["steps"][-1]["run"].split(" >> ")[0]
    (tmp_path / "json.py").write_text('raise RuntimeError("untrusted checkout module executed")')
    (tmp_path / "pathlib.py").write_text('raise RuntimeError("untrusted checkout module executed")')
    policy = tmp_path / ".github/workflows/trusted-actors.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"actors": ACTORS, "queue_actor": "github-merge-queue[bot]"}))
    result = subprocess.run([sys.executable, *shlex.split(command)[1:]], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    outputs = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert json.loads(outputs["actors"]) == ACTORS + ["github-merge-queue[bot]"]
    assert json.loads(outputs["queue_actor"]) == "github-merge-queue[bot]"


@pytest.mark.parametrize("override", ["extra-step", "replace-command", "change-ref", "change-output", "workflow-env"])
def test_loader_cannot_override_protected_actor_data(override):
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    loader = yaml.safe_load((root / ".github/workflows/runner-trust.yml").read_text())
    job = loader["jobs"]["actors"]
    if override == "extra-step":
        job["steps"].append({"run": 'echo \'actors=["stranger"]\' >> "$GITHUB_OUTPUT"'})
    elif override == "replace-command":
        job["steps"][-1]["run"] += '\necho \'actors=["stranger"]\' >> "$GITHUB_OUTPUT"'
    elif override == "change-ref":
        job["steps"][0]["with"]["ref"] = "${{ github.sha }}"
    elif override == "change-output":
        job["outputs"]["actors"] = '["stranger"]'
    else:
        loader["env"] = {"GITHUB_OUTPUT": "override"}
    problems = ci_runner_trust.check({"runner-trust.yml": loader}, ACTORS)
    assert any(isinstance(problem, str) and "runner-trust.yml" in problem.lower() for problem in problems)


@pytest.mark.parametrize(
    "actors",
    [
        tuple(ACTORS),
        [],
        ["operator", "app[bot]", "app[bot]"],
        ["operator", "second", "app[bot]"],
        ["invalid/name", "one[bot]", "two[bot]"],
        [7, "one[bot]", "two[bot]"],
    ],
)
def test_malformed_actor_approvals_fail_closed(actors):
    with pytest.raises(ValueError, match="trusted actors"):
        ci_runner_trust.validate_actors(actors)


def test_workflow_gate_validates_its_actor_input():
    with pytest.raises(ValueError, match="trusted actors"):
        ci_runner_trust.check({}, ACTORS + ["stranger"])


def test_approved_usernames_can_contain_uppercase():
    assert ci_runner_trust.validate_actors(["Operator", "One[bot]", "Two[bot]"]) == ["Operator", "One[bot]", "Two[bot]"]


def test_separately_scoped_queue_identity_can_contain_uppercase():
    policy = {"actors": ACTORS, "queue_actor": "Queue[bot]"}
    assert ci_runner_trust.validate_actors(policy) == ACTORS + ["Queue[bot]"]


def selector(pool="anton-oss"):
    return (
        "${{ (github.event_name != 'pull_request_target' && "
        "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
        'contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.actor) && '
        'contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor) || '
        "(github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev') && "
        "github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]') && '"
        + pool
        + "' || 'ubuntu-latest' }}"
    )


@pytest.mark.parametrize("pool", ["anton-oss", "anton-oss-light", "anton-oss-batch"])
def test_safe_pool_selector_passes_without_job_admission_skip(pool):
    candidate = {
        "on": {"pull_request": {}, "push": {}, "merge_group": {}},
        "jobs": {"test": {"runs-on": selector(pool)}},
    }
    assert (
        ci_runner_trust.check({"test.yml": candidate}, {"actors": ACTORS, "queue_actor": "github-merge-queue[bot]"})
        == []
    )


@pytest.mark.parametrize("replace", ["actor", "trigger", "target", "fork", "queue-ref", "outsider", "fallback", "pool"])
def test_selector_cannot_loosen_actor_or_event_policy(replace):
    runner = selector()
    changes = {
        "actor": (
            'contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.actor) && ',
            "",
        ),
        "trigger": (
            'contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor) || ',
            "true || ",
        ),
        "target": ("github.event_name != 'pull_request_target' && ", ""),
        "fork": ("github.event.pull_request.head.repo.full_name == github.repository", "true"),
        "queue-ref": ("github.ref == 'refs/heads/dev'", "true"),
        "outsider": ('"nestorcolt"', '"stranger"'),
        "fallback": ("'ubuntu-latest'", "'anton-oss'"),
        "pool": ("'anton-oss'", "'anton-other'"),
    }
    before, after = changes[replace]
    runner = runner.replace(before, after)
    assert ci_runner_trust.check(
        {"test.yml": {"jobs": {"test": {"runs-on": runner}}}},
        {"actors": ACTORS, "queue_actor": "github-merge-queue[bot]"},
    )


def test_selector_is_graded_against_base_approvals_instead_of_head_literals():
    policy = {
        "actors": ["different-owner", "agenticore-app[bot]", "tcc-main-ci[bot]"],
        "queue_actor": "github-merge-queue[bot]",
    }
    assert ci_runner_trust.check({"test.yml": {"jobs": {"test": {"runs-on": selector()}}}}, policy)


def test_runner_cli_accepts_the_selector_only_with_protected_policy_metadata(tmp_path):
    import json

    import yaml

    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / "trusted-actors.json").write_text(
        json.dumps({"actors": ACTORS, "queue_actor": "github-merge-queue[bot]"})
    )
    (folder / "selected.yml").write_text(yaml.safe_dump({"jobs": {"test": {"runs-on": selector()}}}))
    assert ci_runner_trust.main(["--root", str(tmp_path)]) == 0


@pytest.mark.parametrize(
    "guard", ["${{ " + GUARD + " }}", "${{" + GUARD + "}}", GUARD + " && (true)", GUARD + " && ((true))"]
)
def test_valid_expression_wrappers_and_tighter_conditions_are_allowed(guard):
    candidate = workflow(guard=guard)
    candidate["jobs"]["build"]["needs"] = "runner-trust"
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS) == []


@pytest.mark.parametrize(
    "guard",
    [
        GUARD.replace("'pull_request_target'", "'workflow_dispatch'"),
        GUARD.replace("'pull_request_target'", "' pull_request_target '"),
        GUARD + " && ",
        GUARD + " && true || true",
        GUARD + " && )(",
        GUARD + " && ((true)",
        GUARD + " && true?",
        GUARD + " && true}",
        GUARD + " && true{",
        GUARD + ' && "true"',
    ],
)
def test_changed_event_literals_and_invalid_additional_conditions_are_red(guard):
    assert ci_runner_trust.check({"build.yml": workflow(guard=guard)}, ACTORS)


def test_unclosed_github_expression_wrapper_is_not_a_valid_actor_guard():
    assert ci_runner_trust.check({"build.yml": workflow(guard="${{" + GUARD + "xx")}, ACTORS)


def test_queue_actor_needs_event_constraints_for_both_identities():
    core_guard = (
        "github.event_name != 'pull_request_target' && "
        "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
        "contains(fromJSON(needs.runner-trust.outputs.actors), github.actor) && "
        "contains(fromJSON(needs.runner-trust.outputs.actors), github.triggering_actor)"
    )
    assert ci_runner_trust.check({"build.yml": workflow(guard=core_guard)}, ACTORS)


def test_policy_records_two_apps_and_the_separately_scoped_queue_actor():
    policy = {"actors": ACTORS, "queue_actor": "github-merge-queue[bot]"}
    assert ci_runner_trust.validate_actors(policy) == ACTORS + ["github-merge-queue[bot]"]


@pytest.mark.parametrize(
    "queue", [None, "nestorcolt", "agenticore-app[bot]", "not/a/bot[bot]", "queue\nactors=[stranger]"]
)
def test_invalid_queue_identity_cannot_poison_protected_outputs(queue):
    with pytest.raises(ValueError, match="trusted actors"):
        ci_runner_trust.validate_actors({"actors": ACTORS, "queue_actor": queue})


@pytest.mark.parametrize("identity", ["github.actor", "github.triggering_actor"])
def test_queue_scope_guard_is_required_for_each_actor(identity):
    clause = (
        f"({identity} != fromJSON(needs.runner-trust.outputs.queue_actor) || github.event_name == 'merge_group' || "
        "(github.event_name == 'push' && github.ref == 'refs/heads/dev'))"
    )
    guard = GUARD.replace(clause + " && ", "").removesuffix(" && " + clause)
    assert ci_runner_trust.check({"build.yml": workflow(guard=guard)}, ACTORS)


@pytest.mark.parametrize("replacement", ["'pull_request'", "'workflow_dispatch'"])
def test_queue_actor_cannot_be_admitted_for_other_events(replacement):
    guard = GUARD.replace("'merge_group'", replacement)
    assert ci_runner_trust.check({"build.yml": workflow(guard=guard)}, ACTORS)


def test_queue_actor_cannot_be_admitted_on_an_unprotected_push_ref():
    guard = GUARD.replace("'refs/heads/dev'", "'refs/heads/other'")
    assert ci_runner_trust.check({"build.yml": workflow(guard=guard)}, ACTORS)


@pytest.mark.parametrize(
    "policy",
    [
        {"actors": ACTORS},
        {"queue_actor": "github-merge-queue[bot]"},
        {"actors": ACTORS, "queue_actor": "github-merge-queue[bot]", "override": "stranger"},
    ],
)
def test_incomplete_or_extended_policy_is_not_an_approval(policy):
    with pytest.raises(ValueError, match="trusted actors"):
        ci_runner_trust.validate_actors(policy)


def test_queue_policy_cannot_nest_another_policy_as_the_app_list():
    policy = {"actors": {"actors": ACTORS, "queue_actor": "inner[bot]"}, "queue_actor": "outer[bot]"}
    with pytest.raises(ValueError, match="trusted actors"):
        ci_runner_trust.validate_actors(policy)


@pytest.mark.parametrize("events", ["pull_request_target", {True: {"pull_request_target": {}}}])
def test_all_yaml_target_trigger_forms_are_rejected(events):
    candidate = workflow()
    if isinstance(events, dict):
        del candidate["on"]
        candidate.update(events)
    else:
        candidate["on"] = events
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS)


def test_every_job_after_a_hosted_or_reusable_job_is_checked():
    candidate = {
        "jobs": {
            "hosted": {"runs-on": "ubuntu-latest"},
            "call": {"uses": "./.github/workflows/other.yml"},
            "private": {"runs-on": "anton"},
        }
    }
    problems = ci_runner_trust.check(
        {"build.yml": candidate, "other.yml": {"jobs": {"hosted": {"runs-on": "ubuntu-latest"}}}}, ACTORS
    )
    assert len(problems) == 1
    assert "build.yml/private" in problems[0]


def test_unknown_local_reusable_workflow_names_the_ungraded_job():
    problems = ci_runner_trust.check(
        {"build.yml": {"jobs": {"call": {"uses": "./.github/workflows/missing.yml"}}}}, ACTORS
    )
    assert len(problems) == 1
    assert "build.yml/call" in problems[0] and "missing.yml" in problems[0]


def test_loader_name_is_optional_and_does_not_change_admission():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    loader = yaml.safe_load((root / ".github/workflows/runner-trust.yml").read_text())
    del loader["name"]
    assert ci_runner_trust.check({"runner-trust.yml": loader, "empty.yaml": {}}, ACTORS) == []


def test_runner_cli_discovers_both_yaml_suffixes_and_reports_full_counts(tmp_path, monkeypatch, capsys):
    import json

    import yaml

    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / "trusted-actors.json").write_text(json.dumps(ACTORS))
    (folder / "first.yml").write_text(
        yaml.safe_dump({"jobs": {"hosted": {"runs-on": "ubuntu-latest"}, "private": {"runs-on": "anton"}}})
    )
    (folder / "second.yaml").write_text(yaml.safe_dump({"jobs": {"private": {"runs-on": ["self-hosted", "linux"]}}}))
    (folder / "empty.yaml").write_text("")
    monkeypatch.chdir(tmp_path)
    assert ci_runner_trust.main([]) == 1
    output = capsys.readouterr().out
    assert "3 workflows, 3 jobs, 2 problems" in output
    assert "::error::first.yml/private" in output
    assert "::error::second.yaml/private" in output
