import json
from types import SimpleNamespace

import pytest

from scripts.swarm import capacity, cli, host_budget, incidents
from scripts.swarm.host_budget import UNKNOWN, Room
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import SwarmConfig
from scripts.swarm_v2 import deployed
from tests.swarm.test_capacity import _store
from tests.swarm.test_tick import FakeLedger
from tests.test_swarm_v2_deployed_runtime import PoolPods, _policy, _worker_pod, _workers

pytestmark = pytest.mark.unit

REAL_HOST = HerdrRuntime.host

DEPLOYED = {"AGENTIHOOKS_DEPLOYMENT": "distributed"}
NODE_OFF = (
    "host room off: a deployed controller's own node says nothing about worker capacity and no worker pool "
    "reading is wired, so spawns pass"
)
PRESSURE_OFF = (
    "host pressure alert off: a deployed controller's own node says nothing about worker capacity; "
    "the host room line shows the worker pool reading or why it is off"
)
WORKERS_OFF = (
    "host room off: the Kubernetes runtime is not configured on this deployed controller, "
    "so the worker pool cannot be read and spawns pass"
)


@pytest.fixture
def no_node(monkeypatch):
    def own_node(*args, **kwargs):
        raise AssertionError("the deployed controller read its own node")

    monkeypatch.setattr(host_budget, "read_host", own_node)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),
        ({"AGENTIHOOKS_DEPLOYMENT": "local"}, False),
        (DEPLOYED, True),
        ({"AGENTIHOOKS_DEPLOYMENT": "compose"}, True),
    ],
)
def test_deployed_mode_is_any_deployment_other_than_local(environ, expected):
    assert host_budget.deployed(environ) is expected


def test_the_deployed_controller_host_room_is_off_without_reading_its_own_node(no_node, monkeypatch):
    monkeypatch.setattr(HerdrRuntime, "host", REAL_HOST)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    assert HerdrRuntime().host() == Room(None, NODE_OFF, UNKNOWN)


def test_a_local_controller_host_room_still_reads_its_own_node(monkeypatch):
    sample = host_budget.HostSample(load1=0.5, cpus=8, available_mb=64000, agents=2)
    monkeypatch.setattr(host_budget, "read_host", lambda: sample)
    monkeypatch.setattr(HerdrRuntime, "host", REAL_HOST)
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    assert HerdrRuntime().host() is sample


def test_status_shows_the_deployed_host_room_off_reason_from_the_capacity_decision(no_node, monkeypatch):
    from scripts.swarm import quota_view

    monkeypatch.setattr(HerdrRuntime, "host", REAL_HOST)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [])
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    decision = HerdrRuntime().quota_capacity(config, [], 100, {"eng": 1, "ci": 0, "plan": 0})
    assert quota_view.host_line(decision["host"]) == f"host room unknown: {NODE_OFF}"


def test_deployed_host_pressure_reads_no_node_and_status_names_the_off_reason(no_node):
    store = _store()
    assert incidents.host_pressure(store, "sw", DEPLOYED) == []
    assert incidents.status_lines(store, "sw") == [PRESSURE_OFF]


def test_local_host_pressure_clears_the_off_reason(monkeypatch):
    store = _store()
    incidents.host_pressure(store, "sw", DEPLOYED)
    monkeypatch.setattr(host_budget, "read_host", lambda: None)
    assert incidents.host_pressure(store, "sw", {}) == []
    assert incidents.status_lines(store, "sw") == []


def test_status_prints_the_host_pressure_off_reason(no_node, monkeypatch, capsys):
    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    incidents.host_pressure(store, "sw", DEPLOYED)
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    assert PRESSURE_OFF in capsys.readouterr().out.splitlines()


def test_deployed_live_inputs_read_the_worker_pool_host(no_node, monkeypatch):
    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [])
    pool, seen = object(), []
    monkeypatch.setattr(deployed, "worker_host", lambda environ, slug: seen.append((environ, slug)) or pool)
    inputs = capacity.live_inputs("sw", store, FakeLedger([]), DEPLOYED, 5_000)
    assert inputs.host is pool
    assert seen == [(DEPLOYED, "sw")]


def test_worker_host_is_off_with_its_reason_without_the_kubernetes_runtime(no_node):
    assert deployed.worker_host(DEPLOYED, "sw")() == Room(None, WORKERS_OFF, UNKNOWN)


def test_worker_host_is_off_with_the_refusal_when_the_runtime_settings_are_refused(no_node):
    environ = {**DEPLOYED, deployed.API_URL_ENV: "http://control"}
    assert deployed.worker_host(environ, "sw")() == Room(
        None,
        f"{WORKERS_OFF}: the Kubernetes runtime needs {', '.join(deployed.REQUIRED)}",
        UNKNOWN,
    )


def test_worker_host_reads_the_worker_namespace_pods(no_node, tmp_path, monkeypatch):
    pods = PoolPods("swarm-pod-proof", [_worker_pod("Running")])
    apis, slugs = [], []
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: apis.append((environ, namespace)) or pods)
    real = deployed.Workers.from_environ
    monkeypatch.setattr(
        deployed.Workers, "from_environ", lambda environ, slug: slugs.append(slug) or real(environ, slug)
    )
    policy = tmp_path / "pool-policy.json"
    pool = {"pods": 4, "cpu_millis": 8000, "memory_mib": 12288}
    policy.write_text(json.dumps({**json.loads(_policy(tmp_path).read_text()), "worker_pool": pool}))
    environ = {**DEPLOYED, **_workers(tmp_path, **{deployed.POLICY_ENV: str(policy)})}
    room = deployed.worker_host(environ, "sw")()
    assert apis == [(environ, json.loads(policy.read_text())["namespace"])]
    assert slugs == ["sw"]
    assert pods.selectors == [""]
    assert (room.room, room.limit) == (2, "memory")


def test_worker_pools_are_unread_without_the_kubernetes_runtime_or_with_refused_settings():
    assert deployed.worker_pools(DEPLOYED, "sw") is None
    assert deployed.worker_pools({**DEPLOYED, deployed.API_URL_ENV: "http://control"}, "sw") is None


def test_worker_pools_read_the_declared_worker_pool_in_the_worker_namespace(tmp_path, monkeypatch):
    placed = {**_worker_pod("Running"), "spec": {**_worker_pod("Running")["spec"], "nodeName": "n1"}}
    pods = PoolPods("swarm-pod-proof", [placed])
    apis, slugs = [], []
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: apis.append((environ, namespace)) or pods)
    real = deployed.Workers.from_environ
    monkeypatch.setattr(
        deployed.Workers, "from_environ", lambda environ, slug: slugs.append(slug) or real(environ, slug)
    )
    policy = tmp_path / "pool-policy.json"
    declared = json.loads(_policy(tmp_path).read_text())
    environ = {**DEPLOYED, **_workers(tmp_path, **{deployed.POLICY_ENV: str(policy)})}
    policy.write_text(json.dumps(declared))
    assert deployed.worker_pools(environ, "sw") is None
    policy.write_text(json.dumps({**declared, "worker_pool": {"pods": 4, "cpu_millis": 8000, "memory_mib": 12288}}))
    assert deployed.worker_pools(environ, "sw")() == [
        {"pool": declared["namespace"], "configured_maximum": 4, "nodes": 1}
    ]
    assert slugs == ["sw", "sw"]
    assert apis == [(environ, declared["namespace"])]


def test_the_autoscale_command_on_a_deployed_controller_names_the_off_reason(no_node, monkeypatch, capsys):
    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    for name, value in DEPLOYED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(deployed.API_URL_ENV, raising=False)
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [])
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    cli.cmd_autoscale(store, SimpleNamespace(slug="sw", fixture=None, json=False))
    assert f"host room None: {WORKERS_OFF}" in capsys.readouterr().out.splitlines()
