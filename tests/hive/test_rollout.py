import copy
import importlib

import pytest


class Cluster:
    def __init__(self):
        self.lease = None
        self.calls = []
        self.healthy = True

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if "deployments" in path:
            deployment = {
                "metadata": {"name": "hive-worker-hive", "namespace": "hive", "generation": 2},
                "spec": {"replicas": 1},
                "status": {
                    "observedGeneration": 2,
                    "updatedReplicas": 1,
                    "readyReplicas": int(self.healthy),
                    "availableReplicas": int(self.healthy),
                },
            }
            return {"items": [deployment]} if "?" in path else deployment
        if method == "GET":
            return copy.deepcopy(self.lease)
        if method == "POST" and self.lease is not None:
            raise importlib.import_module("scripts.hive.rollout").Conflict()
        self.lease = copy.deepcopy(body)
        self.lease["metadata"]["resourceVersion"] = str(len(self.calls))
        return copy.deepcopy(self.lease)


def rollout():
    return importlib.import_module("scripts.hive.rollout")


def test_a_waiting_release_cannot_take_ownership_or_mutate_a_hive():
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    with pytest.raises(TimeoutError):
        module.acquire(cluster, "default", "shared", "second", timeout=0)
    assert cluster.lease["spec"]["holderIdentity"] == "first"
    assert not any(
        method == "DELETE" or ("deployments" in path and method != "GET") for method, path, _ in cluster.calls
    )


def test_a_failed_replacement_holds_the_lock_without_expiry():
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    cluster.healthy = False
    with pytest.raises(RuntimeError, match="serving"):
        module.release(cluster, "default", "shared", "first", "hive", "hive-worker-hive")
    assert cluster.lease["spec"]["holderIdentity"] == "first"
    assert "leaseDurationSeconds" not in cluster.lease["spec"]


def test_only_the_owner_can_release_after_the_current_generation_serves():
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    with pytest.raises(RuntimeError, match="owner"):
        module.release(cluster, "default", "shared", "second", "hive", "hive-worker-hive")
    module.release(cluster, "default", "shared", "first", "hive", "hive-worker-hive")
    module.acquire(cluster, "default", "shared", "second", timeout=0)
    assert cluster.lease["spec"]["holderIdentity"] == "second"


def test_serving_rejects_old_generations_and_old_replicas():
    module = rollout()
    deployment = Cluster().request("GET", "/deployments/own")
    assert module.serving(deployment)
    for key, value in [
        ("observedGeneration", 1),
        ("updatedReplicas", 0),
        ("availableReplicas", 0),
        ("readyReplicas", 0),
    ]:
        stale = copy.deepcopy(deployment)
        stale["status"][key] = value
        assert not module.serving(stale)


def test_acquisition_conflict_reads_the_winner_instead_of_overwriting_it():
    module = rollout()
    cluster = Cluster()
    original = cluster.request

    def race(method, path, body=None):
        if method == "POST":
            winner = copy.deepcopy(body)
            winner["spec"]["holderIdentity"] = "winner"
            original(method, path, winner)
            raise module.Conflict()
        return original(method, path, body)

    cluster.request = race
    with pytest.raises(TimeoutError):
        module.acquire(cluster, "default", "shared", "loser", timeout=0)
    assert cluster.lease["spec"]["holderIdentity"] == "winner"


def test_readiness_needs_a_recent_successful_heartbeat(tmp_path):
    module = rollout()
    marker = tmp_path / "ready"
    assert not module.ready(marker, now=100)
    marker.touch()
    import os

    os.utime(marker, (100, 100))
    assert module.ready(marker, now=110)
    assert not module.ready(marker, now=160)
    assert not module.ready(marker, now=99)


def test_a_release_checks_every_worker_before_releasing_ownership():
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    original = cluster.request

    def unhealthy_second(method, path, body=None):
        result = original(method, path, body)
        if path.endswith("/second"):
            result["status"]["readyReplicas"] = 0
        return result

    cluster.request = unhealthy_second
    with pytest.raises(RuntimeError, match="serving"):
        module.release(cluster, "default", "shared", "first", "hive", "first,second")
    assert cluster.lease["spec"]["holderIdentity"] == "first"


@pytest.mark.parametrize("status", [200, 201, 404, 409, 500])
def test_kubernetes_transport_preserves_api_failures(monkeypatch, status):
    from types import SimpleNamespace

    from scripts.swarm_v2.kubernetes.client import KubeHttp

    module = rollout()
    calls = []
    body = {"spec": {"holderIdentity": "owner"}}
    transport = SimpleNamespace(send=lambda *args: calls.append(args) or (status, body))
    monkeypatch.setattr(KubeHttp, "in_cluster", lambda environ: transport)
    client = module.Kubernetes()
    if status in (200, 201):
        assert client.request("PUT", "/lease", body) == body
    elif status == 404:
        assert client.request("GET", "/lease") is None
        with pytest.raises(RuntimeError, match="404"):
            client.request("PUT", "/lease", body)
    elif status == 409:
        with pytest.raises(module.Conflict):
            client.request("PUT", "/lease", body)
    else:
        with pytest.raises(RuntimeError, match="500"):
            client.request("GET", "/lease")
    assert calls[0][1] == "/lease"


def test_owner_retry_and_waiting_retry_preserve_the_same_lease(monkeypatch):
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    writes = len([call for call in cluster.calls if call[0] != "GET"])
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    assert len([call for call in cluster.calls if call[0] != "GET"]) == writes
    slept = []

    def recovered(seconds):
        slept.append(seconds)
        module.release(cluster, "default", "shared", "first", "hive", "own")

    monkeypatch.setattr(module.time, "sleep", recovered)
    module.acquire(cluster, "default", "shared", "next")
    assert slept == [5]
    assert cluster.lease["spec"]["holderIdentity"] == "next"
    assert cluster.calls[-1][2]["metadata"]["resourceVersion"]


def test_missing_lease_and_missing_replacement_never_release_ownership():
    module = rollout()
    cluster = Cluster()
    with pytest.raises(RuntimeError, match="owner"):
        module.release(cluster, "default", "shared", "first", "hive", "own")
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    original = cluster.request
    cluster.request = lambda method, path, body=None: None if "deployments" in path else original(method, path, body)
    with pytest.raises(RuntimeError, match="serving"):
        module.release(cluster, "default", "shared", "first", "hive", "own")
    assert cluster.lease["spec"]["holderIdentity"] == "first"


@pytest.mark.parametrize("action", ["acquire", "release", "ready"])
def test_hook_entrypoint_routes_to_the_exclusive_protocol(monkeypatch, tmp_path, action):
    import sys

    module = rollout()
    cluster = Cluster()
    monkeypatch.setattr(module, "Kubernetes", lambda: cluster)
    monkeypatch.setattr(
        sys, "argv", ["rollout", action, "--owner", "first", "--hive-namespace", "hive", "--deployment", "own"]
    )
    if action == "release":
        module.acquire(cluster, "default", "agentihooks-hive-rollout", "first", timeout=0)
    if action == "ready":
        marker = tmp_path / "ready"
        monkeypatch.setenv("HIVE_READY_FILE", str(marker))
        with pytest.raises(SystemExit) as stopped:
            module.main()
        assert stopped.value.code == 1
        marker.touch()
        with pytest.raises(SystemExit) as stopped:
            module.main()
        assert stopped.value.code == 0
        assert cluster.calls == []
    else:
        module.main()
        assert cluster.lease["spec"]["holderIdentity"] == ("first" if action == "acquire" else "")
