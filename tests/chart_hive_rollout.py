import subprocess
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


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
        "workers.rollout.enabled=true",
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


def test_disabled_rollouts_do_not_require_a_new_worker_image():
    objects = render("workers.rollout.enabled=false")
    assert not [item for item in objects if item["kind"] == "Job"]
    worker = next(item for item in objects if item["kind"] == "Deployment")
    container = worker["spec"]["template"]["spec"]["containers"][0]
    assert "readinessProbe" not in container
    assert worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 3630
    assert "--wait 3600" in container["lifecycle"]["preStop"]["exec"]["command"][-1]
    assert not any(item["name"] == "HIVE_READY_FILE" for item in container["env"])
    assert yaml.safe_load((CHART / "values.yaml").read_text())["workers"]["rollout"]["enabled"] is False


def test_storage_preparation_is_not_suppressed_by_native_rollout_hooks():
    objects = render("workers.node=node-one", "hiveStorage.default.homes=/homes")
    preparation = next(
        item for item in objects if item["kind"] == "Job" and item["metadata"]["name"].endswith("-storage")
    )
    annotations = preparation["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "PreSync"
    assert int(annotations["argocd.argoproj.io/sync-wave"]) < -1
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation,HookSucceeded"


def test_a_drain_that_consumes_the_rollout_budget_is_refused():
    try:
        render("workers.rollout.drainSeconds=600")
    except subprocess.CalledProcessError as error:
        assert "workers.rollout.drainSeconds must be between 0 and 300" in error.stderr
    else:
        raise AssertionError("an hour long drain must not render")


if __name__ == "__main__":
    test_every_roll_acquires_before_sync_and_releases_after_serving()
    test_hives_in_one_release_have_distinct_sync_waves()
    test_a_drain_that_consumes_the_rollout_budget_is_refused()
    test_disabled_rollouts_do_not_require_a_new_worker_image()
    test_storage_preparation_is_not_suppressed_by_native_rollout_hooks()
    print("Shared rollout hooks, sequential hive waves, readiness and bounded drain: passed")
