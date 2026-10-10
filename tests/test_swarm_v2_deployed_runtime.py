import json
import socket
from functools import partial
from pathlib import Path

import pytest

from scripts.hive import auth as hive_auth
from scripts.swarm import controller as loop
from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import deployed
from scripts.swarm_v2.auth_context import GrantRefused
from scripts.swarm_v2.kubernetes.adapter import KubernetesRuntime
from scripts.swarm_v2.kubernetes.grants import PodGrants
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, load_policy
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL, owner_for
from scripts.swarm_v2.launch import DistributedLaunch, LaunchTerms
from scripts.swarm_v2.runtime.base import LOCAL, SpawnRequest
from scripts.swarm_v2.runtime.routed import RoutedRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "control-fixture"
SEAT = f"eng-1@{SLUG}"
API_URL = "http://swarm-api.agentihooks-swarm.svc:8780"
IMAGE = "sha256:" + "4b" * 32
PROJECT = "github.com/the-cloud-clockwork/agentihooks"
ACCOUNT = "claude-fixture@example.com"
POLICY = Path(__file__).parent / "fixtures" / "swarm_v2" / "pod-policy.json"


def _cs():
    from scripts.swarm_v2 import control_service

    return control_service


class Pods:
    def __init__(self, namespace):
        self.namespace, self.created, self.maps, self.nodes_read = namespace, [], [], 0

    def create_pod(self, body):
        self.created.append(body)
        return self.read_pod(body["metadata"]["name"])

    def read_pod(self, name):
        found = [body for body in self.created if body["metadata"]["name"] == name]
        return {**found[0], "metadata": {**found[0]["metadata"], "uid": "uid-1"}} if found else None

    def list_pods(self, selector):
        return []

    def ready_nodes(self):
        self.nodes_read += 1
        return []

    def create_config_map(self, body):
        self.maps.append(body)
        return body


def _store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
    return store


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _policy(tmp_path, owner=owner_for(SLUG)):
    path = tmp_path / "pod-policy.json"
    path.write_text(json.dumps({**json.loads(POLICY.read_text()), "owner": owner}))
    return path


def _workers(tmp_path, **changes):
    environ = {
        deployed.API_URL_ENV: API_URL,
        deployed.POLICY_ENV: str(_policy(tmp_path)),
        deployed.IMAGE_ENV: IMAGE,
        deployed.PROFILE_ENV: "general",
        deployed.ACCOUNT_ENV: ACCOUNT,
        deployed.CAP_ENV: "2",
        deployed.PROJECTS_ENV: f" {PROJECT} ,",
        deployed.BRAIN_ENV: "swarm",
    }
    return {name: value for name, value in {**environ, **changes}.items() if value is not None}


def _environ(tmp_path, store, **changes):
    key = tmp_path / "launch.key"
    key.write_bytes(b"k" * 32)
    return {
        _cs().KEY_ID_ENV: "launch-1",
        _cs().KEY_FILE_ENV: str(key),
        _cs().PORT_ENV: str(_free_port()),
        _cs().SWARM_ENV: SLUG,
        _cs().CREDENTIAL_ENV: hive_auth.issue_controller(store.redis),
        **_workers(tmp_path, **changes),
    }


def test_the_controller_start_hands_the_tick_the_kubernetes_runtime_and_the_distributed_launch(tmp_path, monkeypatch):
    store = _store()
    for name, value in {**_environ(tmp_path, store), "SWARM_HIVE_ID": "hive-fixture"}.items():
        monkeypatch.setenv(name, value)
    pods, hosted, seen, apis = Pods("swarm-pod-proof"), [], [], []
    monkeypatch.setattr(
        deployed,
        "pod_api",
        lambda environ, namespace: apis.append((environ.get(deployed.API_URL_ENV), namespace)) or pods,
    )
    real_host = _cs().host
    monkeypatch.setattr(_cs(), "host", lambda *args: hosted.append(real_host(*args)) or hosted[0])
    monkeypatch.setattr(loop, "connect", lambda: store)
    monkeypatch.setattr("scripts.operator_env.fill", lambda env: None)

    def tick(given, runtimes):
        service = hosted[0]
        runtime = runtimes[SLUG]
        task = {"id": "t1", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        placed = runtime.spawn(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
        seen.append((given, list(runtimes), service, runtime, placed))
        return {SLUG: ["ticked"]}

    monkeypatch.setattr(loop, "run_once", tick)

    assert loop.main(["run", "--once"]) == 0

    [(given, slugs, service, runtime, placed)] = seen
    assert (given, slugs) == (store, [SLUG])
    assert isinstance(runtime, RoutedRuntime)
    assert list(runtime.router.runtimes) == [LOCAL, BACKEND]
    kubernetes = runtime.router.runtimes[BACKEND]
    assert isinstance(kubernetes, KubernetesRuntime)
    assert kubernetes.execute == service.controller.execute
    assert isinstance(runtime.launch, partial)
    launcher = runtime.launch.func.__self__
    assert isinstance(launcher, DistributedLaunch)
    assert runtime.launch.func == launcher.from_tick
    assert apis == [(API_URL, "swarm-pod-proof"), (API_URL, "swarm-pod-proof")]
    assert isinstance(launcher.homes, PodGrants)
    assert (launcher.homes.api, launcher.homes.owner) == (pods, owner_for(SLUG))
    assert (launcher.capacity.slug, launcher.fleet.slug) == (SLUG, SLUG)
    for authorize in (launcher.capacity.authorize, launcher.fleet.authorize):
        with pytest.raises(GrantRefused) as refused:
            authorize("not-a-grant")
        assert str(refused.value) == "launch grant is malformed"
    assert (launcher.controller, launcher.grants, launcher.router) == (
        service.controller,
        service.grants,
        runtime.router,
    )
    workers = runtime.launch.keywords["target"].__self__
    assert runtime.launch.keywords == {
        "terms": LaunchTerms(ACCOUNT, 2, 300_000, (PROJECT,), "swarm", API_URL),
        "target": workers.target,
    }
    assert store.config(SLUG).api_url == API_URL
    assert (placed.placement, placed.harness, placed.profile) == (BACKEND, "claude", "general")
    [pod] = pods.created
    execution = pod["metadata"]["labels"][EXECUTION_LABEL]
    admitted = store.execution(SLUG, execution)
    assert (admitted.seat, admitted.runtime_backend) == (SEAT, BACKEND)
    assert admitted.runtime_target == {"pod_namespace": "swarm-pod-proof", "pod_name": "swarm-t1"}
    assert pods.nodes_read == 1
    assert lease.current(store, SLUG) is None


def test_the_launch_record_names_the_admitted_execution_and_the_worker_settings(tmp_path):
    workers = deployed.Workers.from_environ(_workers(tmp_path), SLUG)
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=API_URL)
    task = {
        "id": "t1",
        "seat": SEAT,
        "controller_epoch": 4,
        "execution_id": "exe-" + "1" * 32,
        "generation": 1,
        "endpoints": {"api_url": API_URL},
        "launch_grant": "never-copied",
    }
    record = workers.launch(SpawnRequest(config, "eng", "engineer-1", task))

    assert record == {
        "task_id": "t1",
        "swarm_id": SLUG,
        "seat_id": SEAT,
        "grant_ref": "grant-exe-" + "1" * 32,
        "controller_epoch": 4,
        "project_id": PROJECT,
        "harness": "claude",
        "image_digest": IMAGE,
        "profile": "general",
        "memory_mib": 4096,
        "cpu_millis": 2000,
        "provider_account": ACCOUNT,
        "task_payload": {"task_id": "t1", "lane": "eng", "name": "engineer-1", "endpoints": {"api_url": API_URL}},
    }


def test_the_start_runs_as_before_without_an_api_address(tmp_path):
    store = _store()
    environ = _environ(tmp_path, store, **{deployed.API_URL_ENV: None, deployed.POLICY_ENV: None})

    service = _cs().host(environ, store, "hive-fixture")
    try:
        assert service.runtime is None
        assert store.config(SLUG).api_url == ""
    finally:
        service.stop()


def test_a_service_publishes_no_api_address_until_one_is_set(tmp_path):
    store = _store()
    service = _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path, store)), lambda: True)

    assert service.api_url == ""


def test_the_tick_runtime_takes_its_disabled_backends_from_the_control_service_settings(tmp_path, monkeypatch):
    store = _store()
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods(namespace))
    environ = {**_environ(tmp_path, store), "AGENTIHOOKS_RUNTIME_DISABLED": BACKEND}

    service = _cs().host(environ, store, "hive-fixture")
    try:
        assert service.runtime.router.disabled == frozenset({BACKEND})
    finally:
        service.stop()


def test_a_missing_worker_setting_refuses_the_start_and_names_each_one(tmp_path):
    store = _store()
    environ = _environ(tmp_path, store, **{deployed.IMAGE_ENV: None, deployed.BRAIN_ENV: None})

    with pytest.raises(SwarmError) as refused:
        _cs().host(environ, store, "hive-fixture")

    assert str(refused.value) == f"the Kubernetes runtime needs {deployed.IMAGE_ENV}, {deployed.BRAIN_ENV}"
    assert lease.current(store, SLUG) is None


@pytest.mark.parametrize("cap", ["0", "two", "-1"])
def test_a_launch_cap_that_is_not_a_positive_whole_number_is_refused(tmp_path, cap):

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.CAP_ENV: cap}), SLUG)

    assert str(refused.value) == f"{deployed.CAP_ENV} must be a whole number above zero"


def test_a_profile_the_pod_policy_lacks_is_refused(tmp_path):

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.PROFILE_ENV: "gpu"}), SLUG)

    assert str(refused.value) == "the Pod policy has no gpu profile"


def test_a_pod_policy_owned_by_another_swarm_is_refused(tmp_path):
    other = tmp_path / "other-policy.json"
    other.write_text(json.dumps({**json.loads(POLICY.read_text()), "owner": "agentihooks-swarm-other"}))
    environ = _workers(tmp_path, **{deployed.POLICY_ENV: str(other)})

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(environ, SLUG)

    assert str(refused.value) == f"the Pod policy owner must be {owner_for(SLUG)}"


def test_an_invalid_pod_policy_is_refused(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"owner": owner_for(SLUG)}))

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.POLICY_ENV: str(path)}), SLUG)

    with pytest.raises(PodSpecRefused) as expected:
        load_policy(path)
    assert str(refused.value) == str(expected.value)


def test_a_scheduled_pass_gives_each_swarm_its_own_runtime_or_the_shared_one(monkeypatch):
    store = _store()
    store.create(SwarmConfig("other", "agentihooks", 2, 0))
    given = []
    monkeypatch.setattr(
        "scripts.swarm.cli.run_tick",
        lambda store, slug, ledger, runtime, messenger, scheduled: given.append((slug, runtime, scheduled)) or [],
    )

    loop.run_once(store, runtime="shared", runtimes={SLUG: "deployed"})

    assert sorted(given) == [(SLUG, "deployed", True), ("other", "shared", True)]


def test_a_kubernetes_launch_puts_its_grant_in_the_pod_launch_material_and_records_it_handed(tmp_path, monkeypatch):
    store = _store()
    pods = Pods("swarm-pod-proof")
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        runtime = service.runtime
        launcher = runtime.launch.func.__self__
        task = {"id": "t1", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        request = SpawnRequest(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
        target = runtime.launch.keywords["target"](request)
        agent = AgentRecord(request.name, "eng", "t1", seat=SEAT, runtime_backend=BACKEND, runtime_target=target)
        launch = launcher.spawn(request, agent, runtime.launch.keywords["terms"], "")
        claims = service.grants.verify(SLUG, pods.maps[0]["data"]["launch-grant"])
    finally:
        service.stop()

    assert launch.handed is True
    execution = launch.agent.execution_id
    [pod] = pods.created
    assert pod["metadata"]["name"] == f"swarm-{execution}"
    assert pods.maps == [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"swarm-{execution}-launch",
                "namespace": "swarm-pod-proof",
                "labels": {
                    "swarm.agentihooks.io/controller-owner": owner_for(SLUG),
                    EXECUTION_LABEL: execution,
                    "swarm.agentihooks.io/generation": str(launch.agent.generation),
                },
                "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": f"swarm-{execution}", "uid": "uid-1"}],
            },
            "immutable": True,
            "data": {"launch-grant": launch.grant},
        }
    ]
    assert claims.execution_id == execution


def test_the_pod_api_talks_to_the_in_cluster_server_in_the_policy_namespace(monkeypatch):
    environ = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443"}
    monkeypatch.setattr(deployed.KubeHttp, "in_cluster", lambda given: ("in-cluster", given))

    api = deployed.pod_api(environ, "swarm-pod-proof")

    assert isinstance(api, deployed.PodClient)
    assert (api.http, api.namespace) == (("in-cluster", environ), "swarm-pod-proof")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (KeyError("KUBERNETES_SERVICE_HOST"), "the Kubernetes runtime needs KUBERNETES_SERVICE_HOST"),
        (
            FileNotFoundError(2, "No such file or directory"),
            "the service account CA is unreadable: No such file or directory",
        ),
    ],
)
def test_a_controller_outside_a_cluster_is_refused_by_name(monkeypatch, error, message):

    def in_cluster(environ):
        raise error

    monkeypatch.setattr(deployed.KubeHttp, "in_cluster", in_cluster)

    with pytest.raises(SwarmError) as refused:
        deployed.pod_api({}, "swarm-pod-proof")

    assert str(refused.value) == message


@pytest.mark.parametrize("projects", [" , ", ","])
def test_a_project_list_naming_no_project_is_refused(tmp_path, projects):

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.PROJECTS_ENV: projects}), SLUG)

    assert str(refused.value) == f"{deployed.PROJECTS_ENV} names no project"


def test_a_launch_cap_in_non_ascii_digits_is_refused(tmp_path):

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.CAP_ENV: "²"}), SLUG)

    assert str(refused.value) == f"{deployed.CAP_ENV} must be a whole number above zero"


def test_only_the_lease_holder_writes_the_api_address(tmp_path, monkeypatch):
    store = _store()
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods(namespace))
    holder = _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path, store)), lambda: True)
    assert holder.start()

    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        assert service.runtime is not None
        assert store.config(SLUG).api_url == ""
        holder.stop()
        assert service.tick() is True
        assert store.config(SLUG).api_url == API_URL
    finally:
        service.stop()
