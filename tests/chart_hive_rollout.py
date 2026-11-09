import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


def render(*settings: str, chart: Path = CHART) -> list[dict]:
    command = [
        "helm",
        "template",
        "proof",
        str(chart),
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
        assert job["spec"]["ttlSecondsAfterFinished"] == 3600
        pod = job["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is True
        command = pod["containers"][0]["command"]
        assert "scripts.hive.rollout" in command
        assert command[command.index("--namespace") + 1] == "default"
        assert command[command.index("--name") + 1] == "agentihooks-hive-rollout"
        assert command[command.index("--owner") + 1] == "hive/proof"
    worker = next(item for item in objects if item["kind"] == "Deployment")
    pod = worker["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == 1230
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
        render("workers.rollout.drainSeconds=1800")
    except subprocess.CalledProcessError as error:
        assert "workers.rollout.drainSeconds must be between 0 and 1500" in error.stderr
    else:
        raise AssertionError("an hour long drain must not render")


def test_the_release_stamp_annotates_only_leased_hive_pods():
    image = "ghcr.io/the-cloud-clockwork/agentihooks-hive-worker:2.17.471"
    unstamped = next(item for item in render() if item["kind"] == "Deployment")
    assert "agentihooks.io/release" not in unstamped["spec"]["template"]["metadata"]["annotations"]
    stamped = next(item for item in render(f"workers.rollout.release={image}") if item["kind"] == "Deployment")
    assert stamped["spec"]["template"]["metadata"]["annotations"]["agentihooks.io/release"] == image
    unleased = next(
        item
        for item in render(f"workers.rollout.release={image}", "workers.rollout.enabled=false")
        if item["kind"] == "Deployment"
    )
    assert "agentihooks.io/release" not in unleased["spec"]["template"]["metadata"]["annotations"]


def test_a_release_tag_worker_is_not_rolled_by_a_release_image_digest():
    for tag in ("release", "dev", "2.17.486", "v2.17.486"):
        worker = next(item for item in render(f"workers.image.tag={tag}") if item["kind"] == "Deployment")
        assert "agentihooks.io/release" not in worker["spec"]["template"]["metadata"]["annotations"]


def render_released(app_version: str, *settings: str) -> list[dict]:
    """Render a copy of the chart whose appVersion is the one the chart release package stamps."""
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / CHART.name
        shutil.copytree(CHART, copy)
        meta = yaml.safe_load((copy / "Chart.yaml").read_text())
        meta["appVersion"] = app_version
        (copy / "Chart.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
        return render(*settings, chart=copy)


def hook_images(objects: list[dict]) -> set[str]:
    """Name the image every rollout hook Job runs."""
    return {
        item["spec"]["template"]["spec"]["containers"][0]["image"]
        for item in objects
        if item["kind"] == "Job" and item["metadata"]["name"].endswith(("-acquire", "-release"))
    }


def test_a_released_chart_runs_its_hooks_on_the_hive_worker_of_its_own_version() -> None:
    repository = "ghcr.io/the-cloud-clockwork/agentihooks-hive-worker"
    for tag in ("release", "dev", "2.17.400"):
        objects = render_released("2.17.467", f"workers.image.tag={tag}")
        assert hook_images(objects) == {f"{repository}:2.17.467"}
        worker = next(item for item in objects if item["kind"] == "Deployment")
        assert worker["spec"]["template"]["spec"]["containers"][0]["image"] == f"{repository}:{tag}"
    assert hook_images(render_released("dev", "workers.image.tag=release")) == {f"{repository}:release"}
    assert hook_images(render("workers.image.tag=release")) == {f"{repository}:release"}


def test_a_chart_whose_app_version_is_neither_a_release_nor_dev_is_refused() -> None:
    try:
        render_released("v2.17.467")
    except subprocess.CalledProcessError as error:
        assert "appVersion must be a release version" in error.stderr
    else:
        raise AssertionError("a malformed appVersion must not fall back to the floating worker tag")


if __name__ == "__main__":
    test_every_roll_acquires_before_sync_and_releases_after_serving()
    test_hives_in_one_release_have_distinct_sync_waves()
    test_a_drain_that_consumes_the_rollout_budget_is_refused()
    test_disabled_rollouts_do_not_require_a_new_worker_image()
    test_storage_preparation_is_not_suppressed_by_native_rollout_hooks()
    test_the_release_stamp_annotates_only_leased_hive_pods()
    test_a_release_tag_worker_is_not_rolled_by_a_release_image_digest()
    test_a_released_chart_runs_its_hooks_on_the_hive_worker_of_its_own_version()
    test_a_chart_whose_app_version_is_neither_a_release_nor_dev_is_refused()
    print("Shared rollout hooks, sequential hive waves, readiness and bounded drain: passed")
