import subprocess
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[2] / "deploy/helm/agentihooks-swarm"


def render(*settings):
    command = [
        "helm",
        "template",
        "proof",
        str(CHART),
        "--namespace",
        "hive",
        "--set",
        "hiveOnly=true",
        "--set",
        "workers.replicas=1",
        "--set",
        "workers.credentialSecret.name=member",
        "--set",
        "workers.credentialSecret.key=credential",
    ]
    for setting in settings:
        command.extend(["--set", setting])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def test_every_roll_acquires_before_sync_and_releases_after_serving():
    objects = render()
    jobs = [item for item in objects if item["kind"] == "Job"]
    assert {item["metadata"]["annotations"]["argocd.argoproj.io/hook"] for item in jobs} == {"PreSync", "PostSync"}
    for job in jobs:
        pod = job["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is True
        command = pod["containers"][0]["command"]
        assert "scripts.hive.rollout" in command
        assert command[command.index("--namespace") + 1] == "default"
        assert command[command.index("--name") + 1] == "agentihooks-hive-rollout"
        assert command[command.index("--owner") + 1] == "hive/proof"
    worker = next(item for item in objects if item["kind"] == "Deployment")
    pod = worker["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == 330
    assert "ready" in pod["containers"][0]["readinessProbe"]["exec"]["command"]
    assert pod["automountServiceAccountToken"] is False
    permissions = [item for item in objects if item["kind"] in ("Role", "RoleBinding", "ServiceAccount")]
    assert permissions
    assert all(item["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PreSync" for item in permissions)
    assert all(int(item["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < -1 for item in permissions)


def test_hives_in_one_release_have_distinct_sync_waves():
    objects = render(
        "workers.hives[0].name=second",
        "workers.hives[0].credentialSecret.name=second",
        "workers.hives[0].credentialSecret.key=credential",
    )
    workers = [item for item in objects if item["kind"] == "Deployment"]
    assert len(workers) == 2
    waves = [item["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] for item in workers]
    assert len(set(waves)) == 2
    release = next(
        item
        for item in objects
        if item["kind"] == "Job" and item["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    )
    command = release["spec"]["template"]["spec"]["containers"][0]["command"]
    assert set(command[command.index("--deployment") + 1].split(",")) == {item["metadata"]["name"] for item in workers}


def test_a_drain_that_consumes_the_rollout_budget_is_refused():
    try:
        render("workers.drainSeconds=600")
    except subprocess.CalledProcessError as error:
        assert "workers.drainSeconds must be between 0 and 300" in error.stderr
    else:
        raise AssertionError("an hour long drain must not render")
