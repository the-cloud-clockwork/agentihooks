import json
from pathlib import Path

import pytest

from scripts.swarm_v2.artifacts import base, benchmark, publication
from scripts.swarm_v2.artifacts.local import LocalBackend
from scripts.swarm_v2.kubernetes import storage
from scripts.swarm_v2.kubernetes.spec import PodTemplate, load_policy
from tests.contracts.storage_layout.test_publication import DATA, SCOPE
from tests.test_swarm_v2_cache import stamped

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
LAUNCH = Path(__file__).parents[2] / "fixtures" / "swarm_v2" / "pod-launch.json"
REFUSED = {
    "shared-codex-home-pod.json": "shared_runtime",
    "operator-home-pod.json": "operator_home",
    "writable-node-cache-pod.json": "unscoped_shared_write",
}


def test_the_committed_safe_pod_is_what_the_shared_policy_renders():
    rendered = PodTemplate(load_policy(FIXTURES / "pod-policy-shared.json")).render(json.loads(LAUNCH.read_text()))
    assert rendered.pod == json.loads((FIXTURES / "safe-pod.json").read_text())


def test_the_committed_safe_pod_passes_the_checker_command():
    assert storage.main([str(FIXTURES / "safe-pod.json")]) == 0


@pytest.mark.parametrize("name, reason", sorted(REFUSED.items()))
def test_each_committed_unsafe_pod_reproduces_its_refusal(name, reason):
    checker = storage.MountChecker()
    with pytest.raises(storage.StorageRefused) as error:
        checker.check(json.loads((FIXTURES / name).read_text()))
    assert error.value.reason == reason
    assert storage.main([str(FIXTURES / name)]) == 1


def test_switching_publishing_off_pauses_without_touching_the_backend(tmp_path):
    (tmp_path / "attempt.bin").write_bytes(DATA)
    shared = tmp_path / "shared"
    shared.mkdir()
    store = base.ArtifactStore(LocalBackend(shared))
    off = publication.publish(store, SCOPE, "ckpt-1", tmp_path / "attempt.bin", {publication.SWITCH: "off"})
    assert off == publication.Publication(
        publication.PAUSED,
        None,
        "SWARM_ARTIFACT_PUBLISHING is off, so publication is paused and the attempt keeps its files",
    )
    assert list(shared.iterdir()) == []


@pytest.mark.parametrize("value", ["on", "", "OFF", "0"])
def test_only_the_word_off_switches_publishing_off(tmp_path, value):
    (tmp_path / "attempt.bin").write_bytes(DATA)
    store = base.ArtifactStore(LocalBackend(tmp_path / "shared"))
    result = publication.publish(store, SCOPE, "ckpt-1", tmp_path / "attempt.bin", {publication.SWITCH: value})
    assert result.state == publication.PUBLISHED


def test_rollback_rehearsal_pauses_then_resumes_publication_with_the_attempt_intact(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "diff.patch").write_bytes(DATA)
    before = stamped(attempt)
    store = base.ArtifactStore(LocalBackend(tmp_path / "shared"))
    paused = publication.publish(store, SCOPE, "ckpt-1", attempt / "diff.patch", {publication.SWITCH: "off"})
    resumed = publication.publish(store, SCOPE, "ckpt-1", attempt / "diff.patch", {})
    assert (paused.state, resumed.state) == (publication.PAUSED, publication.PUBLISHED)
    assert resumed.ref == base.ArtifactRef.of(DATA)
    assert stamped(attempt) == before


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.25
        return self.now


def test_the_benchmark_reports_throughput_and_loss_and_removes_its_folder(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    report = benchmark.run(mount, [1, 2], 2, Clock())
    assert report["root"] == str(mount)
    assert report["throughput"] == [
        {"size_mib": 1, "rounds": 2, "seconds": 0.5, "mib_per_s": 4.0},
        {"size_mib": 2, "rounds": 2, "seconds": 0.5, "mib_per_s": 8.0},
    ]
    loss = report["loss"]
    assert (loss["while_lost"], loss["source_intact"], loss["after_restore"]) == ("paused", True, "published")
    assert loss["reason"].startswith("artifact storage is unavailable (")
    assert list(mount.iterdir()) == []


def test_the_benchmark_writes_each_round_as_its_own_artifact(tmp_path):
    store = base.ArtifactStore(LocalBackend(tmp_path))
    benchmark.throughput(store, [1], 3, Clock())
    assert [store.recorded(benchmark.SCOPE, f"bench-1-{n}").size for n in range(3)] == [benchmark.MIB] * 3


def test_the_benchmark_command_prints_its_report(tmp_path, capsys):
    assert benchmark.main([str(tmp_path), "--sizes", "1", "--rounds", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [row["size_mib"] for row in report["throughput"]] == [1]
    assert report["loss"]["after_restore"] == "published"


def test_the_benchmark_command_refuses_a_missing_folder_and_bad_sizes(tmp_path, capsys):
    assert benchmark.main([str(tmp_path / "absent")]) == 1
    assert capsys.readouterr().err == f"{tmp_path / 'absent'} is not a folder\n"
    assert benchmark.main([str(tmp_path), "--sizes", "one"]) == 64
    assert capsys.readouterr().err == "--sizes must be whole MiB counts: one\n"
    assert benchmark.main([]) == 64
    assert capsys.readouterr().err.startswith("usage: python -m scripts.swarm_v2.artifacts.benchmark")
    assert benchmark.main(["--help"]) == 0
