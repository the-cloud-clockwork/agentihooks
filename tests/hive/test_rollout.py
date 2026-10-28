import copy
import importlib

import pytest


class Cluster:
    def __init__(self):
        self.lease = None
        self.calls = []
        self.healthy = True

    def request(self, method, path, body=None):
        import re

        assert method in ("GET", "POST", "PUT")
        self.calls.append((method, path, copy.deepcopy(body)))
        if "deployments" in path:
            assert method == "GET"
            assert re.fullmatch(r"/apis/apps/v1/namespaces/[^/]+/deployments/[^/]+", path)
            return {
                "metadata": {"name": "hive-worker-hive", "namespace": "hive", "generation": 2},
                "spec": {"replicas": 1},
                "status": {
                    "observedGeneration": 2,
                    "updatedReplicas": 1,
                    "readyReplicas": int(self.healthy),
                    "availableReplicas": int(self.healthy),
                },
            }
        route = re.fullmatch(r"/apis/coordination.k8s.io/v1/namespaces/([^/]+)/leases(?:/([^/]+))?", path)
        assert route is not None
        if method == "GET":
            assert route[2]
            return copy.deepcopy(self.lease)
        assert body["apiVersion"] == "coordination.k8s.io/v1"
        assert body["kind"] == "Lease"
        assert body["metadata"]["namespace"] == route[1]
        if method == "POST":
            assert route[2] is None
            assert body["metadata"]["name"]
            if self.lease is not None:
                raise importlib.import_module("scripts.hive.rollout").Conflict()
        else:
            assert body["metadata"]["name"] == route[2]
            assert body["metadata"]["resourceVersion"] == self.lease["metadata"]["resourceVersion"]
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
    with pytest.raises(RuntimeError, match="^replacement hive is not serving; rollout ownership retained$"):
        module.release(cluster, "default", "shared", "first", "hive", "hive-worker-hive")
    assert cluster.lease["spec"]["holderIdentity"] == "first"
    assert "leaseDurationSeconds" not in cluster.lease["spec"]


def test_only_the_owner_can_release_after_the_current_generation_serves():
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    with pytest.raises(RuntimeError, match="^only the rollout owner can release the lease$"):
        module.release(cluster, "default", "shared", "second", "hive", "hive-worker-hive")
    module.release(cluster, "default", "shared", "first", "hive", "hive-worker-hive")
    module.acquire(cluster, "default", "shared", "second", timeout=0)
    assert cluster.lease["spec"]["holderIdentity"] == "second"


def test_serving_rejects_old_generations_and_old_replicas():
    module = rollout()
    deployment = Cluster().request("GET", "/apis/apps/v1/namespaces/hive/deployments/own")
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
    assert module.ready(marker, now=100)
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
    with pytest.raises(RuntimeError, match="^replacement hive is not serving; rollout ownership retained$"):
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

    def connect(environ):
        import os

        assert environ is os.environ
        return transport

    monkeypatch.setattr(KubeHttp, "in_cluster", connect)
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
    assert calls[0] == (("PUT", "/lease", body) if status in (200, 201, 409) else ("GET", "/lease", None))


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
    with pytest.raises(RuntimeError, match="^only the rollout owner can release the lease$"):
        module.release(cluster, "default", "shared", "first", "hive", "own")
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    original = cluster.request
    cluster.request = lambda method, path, body=None: None if "deployments" in path else original(method, path, body)
    with pytest.raises(RuntimeError, match="^replacement hive is not serving; rollout ownership retained$"):
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


def test_kubernetes_creation_uses_the_exact_lease_envelope_and_collection():
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "rolls", "capacity", "hive/app", timeout=0)
    assert cluster.calls == [
        ("GET", "/apis/coordination.k8s.io/v1/namespaces/rolls/leases/capacity", None),
        (
            "POST",
            "/apis/coordination.k8s.io/v1/namespaces/rolls/leases",
            {
                "apiVersion": "coordination.k8s.io/v1",
                "kind": "Lease",
                "metadata": {"name": "capacity", "namespace": "rolls"},
                "spec": {"holderIdentity": "hive/app"},
            },
        ),
    ]


def test_unobserved_and_zero_capacity_deployments_never_count_as_serving():
    module = rollout()
    template = Cluster().request("GET", "/apis/apps/v1/namespaces/hive/deployments/own")
    defaulted = copy.deepcopy(template)
    del defaulted["spec"]["replicas"]
    assert module.serving(defaulted)
    two = copy.deepcopy(template)
    two["spec"]["replicas"] = 2
    two["status"].update(updatedReplicas=2, readyReplicas=2, availableReplicas=2)
    assert module.serving(two)
    zero = copy.deepcopy(template)
    zero["spec"]["replicas"] = 0
    zero["status"].update(updatedReplicas=0, readyReplicas=0, availableReplicas=0)
    assert not module.serving(zero)
    absent = copy.deepcopy(template)
    del absent["status"]
    assert not module.serving(absent)
    unobserved = copy.deepcopy(template)
    unobserved["metadata"]["generation"] = 1
    del unobserved["status"]["observedGeneration"]
    assert not module.serving(unobserved)
    for field in ("readyReplicas", "availableReplicas", "updatedReplicas"):
        partial = copy.deepcopy(template)
        del partial["status"][field]
        assert not module.serving(partial)


@pytest.mark.parametrize("timeout", [None, 0])
def test_ownership_wait_ends_at_the_exact_deadline(monkeypatch, timeout):
    module = rollout()
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    ticks = iter([0, 1800 if timeout is None else 0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: pytest.fail("slept beyond the acquisition deadline"))
    kwargs = {} if timeout is None else {"timeout": timeout}
    with pytest.raises(TimeoutError) as failed:
        module.acquire(cluster, "default", "shared", "second", **kwargs)
    assert str(failed.value) == "another hive rollout owns the lease; no workload was changed"
    assert cluster.lease["spec"]["holderIdentity"] == "first"


def test_resource_names_are_quoted_as_single_url_segments():
    module = rollout()
    assert (
        module.lease_path("team/one", "roll/two")
        == "/apis/coordination.k8s.io/v1/namespaces/team%2Fone/leases/roll%2Ftwo"
    )
    cluster = Cluster()
    module.acquire(cluster, "default", "shared", "first", timeout=0)
    module.release(cluster, "default", "shared", "first", "team/one", "hive/two")
    assert cluster.calls[-2][:2] == ("GET", "/apis/apps/v1/namespaces/team%2Fone/deployments/hive%2Ftwo")


@pytest.mark.parametrize("action", ["acquire", "release"])
def test_hook_cli_defaults_and_overrides_reach_the_protocol(monkeypatch, action):
    import sys

    module = rollout()
    api, calls = object(), []
    monkeypatch.setattr(module, "Kubernetes", lambda: api)
    monkeypatch.setattr(module, action, lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["rollout", action])
    module.main()
    defaults = (api, "default", "agentihooks-hive-rollout", "")
    assert calls == [defaults if action == "acquire" else (*defaults, "", "")]
    calls.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rollout",
            action,
            "--namespace",
            "rolls",
            "--name",
            "shared",
            "--owner",
            "hive/app",
            "--hive-namespace",
            "workers",
            "--deployment",
            "one,two",
        ],
    )
    module.main()
    override = (api, "rolls", "shared", "hive/app")
    assert calls == [override if action == "acquire" else (*override, "workers", "one,two")]


def test_unknown_hook_actions_never_contact_kubernetes(monkeypatch):
    import sys

    module = rollout()
    monkeypatch.setattr(module, "Kubernetes", lambda: pytest.fail("invalid action contacted Kubernetes"))
    monkeypatch.setattr(sys, "argv", ["rollout", "unknown"])
    with pytest.raises(SystemExit) as failed:
        module.main()
    assert failed.value.code == 2
