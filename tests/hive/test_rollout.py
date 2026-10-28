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
    assert not module.ready(marker, now=200)
