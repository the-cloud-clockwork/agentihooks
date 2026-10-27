import json
import socket
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hooks.context import account_sessions
from scripts.hive import auth as hive_auth
from scripts.swarm import capacity, host_budget, lease
from scripts.swarm import controller as loop
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import SpawnError
from scripts.swarm_v2 import deployed, image_tag, quota
from scripts.swarm_v2.accounts import RESERVED, Slot, account_key, encode
from scripts.swarm_v2.auth_context import GrantRefused
from scripts.swarm_v2.kubernetes import operator_terminal
from scripts.swarm_v2.kubernetes.adapter import KubernetesRuntime
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.kubernetes.grants import PodGrants
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, load_policy, pod_name
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.launch import DistributedLaunch, LaunchTerms
from scripts.swarm_v2.runtime import observe
from scripts.swarm_v2.runtime.base import LOCAL, SpawnRequest
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests import sv2_hdr03_cases

pytestmark = pytest.mark.unit

SLUG = "control-fixture"
SEAT = f"eng-1@{SLUG}"
API_URL = "http://swarm-api.agentihooks-swarm.svc:8780"
IMAGE = "sha256:" + "4b" * 32
REPOSITORY = "ghcr.io/the-cloud-clockwork/agentihooks-worker"
PROJECT = "github.com/the-cloud-clockwork/agentihooks"
ACCOUNT = "claude-fixtureatexample-com"
POLICY = Path(__file__).parent / "fixtures" / "swarm_v2" / "pod-policy.json"


def _cs():
    from scripts.swarm_v2 import control_service

    return control_service


class Pods:
    def __init__(self, namespace, keys=(ACCOUNT,)):
        self.namespace, self.created, self.maps, self.nodes_read = namespace, [], [], 0
        self.objects, self.keys, self.reads = [], keys, []

    def read(self, kind, name):
        self.reads.append((kind, name))
        return {"metadata": {"name": name}, "data": dict.fromkeys(self.keys, "YXBwcm92ZWQ=")}

    def create_pod(self, body):
        self.created.append(body)
        return self.read_pod(body["metadata"]["name"])

    def read_pod(self, name):
        found = [body for body in self.created if body["metadata"]["name"] == name]
        return {**found[0], "metadata": {**found[0]["metadata"], "uid": "uid-1"}} if found else None

    def list_pods(self, selector):
        return []

    def list_quotas(self):
        return []

    def snapshot(self, selector):
        return [], ""

    def ready_nodes(self):
        self.nodes_read += 1
        return []

    def create_config_map(self, body):
        self.maps.append(body)
        return body

    def create_object(self, body):
        self.objects.append(body)
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


def _policy(tmp_path):
    path = tmp_path / "pod-policy.json"
    path.write_text(json.dumps({**json.loads(POLICY.read_text()), "provider_accounts": []}))
    return path


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    asked, digests = [], [IMAGE]

    def resolve(repository, tag):
        asked.append((repository, tag))
        return digests[min(len(asked), len(digests)) - 1]

    monkeypatch.setattr(image_tag, "resolve", resolve)
    return asked, digests


def _workers(tmp_path, **changes):
    environ = {
        deployed.API_URL_ENV: API_URL,
        deployed.POLICY_ENV: str(_policy(tmp_path)),
        deployed.IMAGE_TAG_ENV: "dev",
        deployed.PROFILE_ENV: "general",
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


@pytest.mark.parametrize(("lane", "profile"), [("eng", "engineer"), ("ci", "cicd")])
def test_the_controller_start_hands_the_tick_the_kubernetes_runtime_and_the_distributed_launch(
    tmp_path, monkeypatch, lane, profile
):
    store = _store()
    for name, value in {**_environ(tmp_path, store), "SWARM_HIVE_ID": "hive-fixture"}.items():
        monkeypatch.setenv(name, value)
    pods, hosted, seen, apis, launches = Pods("swarm-pod-proof"), [], [], [], []
    real_launch = deployed.Workers.launch

    def launch(workers, request):
        payload = real_launch(workers, request)
        launches.append(payload)
        return payload

    monkeypatch.setattr(deployed.Workers, "launch", launch)
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
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        task["profile_decision"] = {
            "profile": profile,
            "source": "classifier",
            "responsibility": "task",
            "anchors": [],
            "overlays": [],
        }
        placed = runtime.spawn(store.config(SLUG), lane, store.next_name(SLUG, lane), task)
        seen.append((given, list(runtimes), service, runtime, placed))
        return {SLUG: ["ticked"]}

    monkeypatch.setattr(loop, "run_once", tick)

    assert loop.main(["run", "--once"]) == 0

    [(given, slugs, service, runtime, placed)] = seen
    assert (given, slugs) == (store, [SLUG])
    assert isinstance(runtime, RoutedRuntime)
    assert list(runtime.router.runtimes) == [LOCAL, BACKEND]
    assert runtime.herdr_runtime.defer_master_canary is True
    kubernetes = runtime.router.runtimes[BACKEND]
    assert isinstance(kubernetes, KubernetesRuntime)
    assert kubernetes.execute == service.controller.execute
    assert isinstance(runtime.launch, partial)
    launcher = runtime.launch.func.__self__
    assert isinstance(launcher, DistributedLaunch)
    assert runtime.launch.func == launcher.from_tick
    assert apis == [(API_URL, "swarm-pod-proof")] * 3
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
        "terms": LaunchTerms("", 2, 300_000, (PROJECT,), "swarm", API_URL),
        "target": workers.target,
    }
    assert runtime.relaunch.func == launcher.relaunch
    assert runtime.relaunch.keywords == {"terms": runtime.launch.keywords["terms"]}
    assert service.recover.replacements.relaunch == runtime.relaunch
    assert isinstance(service.recover.replacements.ledger, LedgerClient)
    assert store.config(SLUG).api_url == API_URL
    assert (placed.placement, placed.harness, placed.profile) == (BACKEND, "claude", profile)
    [pod] = pods.created
    execution = pod["metadata"]["labels"][EXECUTION_LABEL]
    admitted = store.execution(SLUG, execution)
    assert (admitted.seat, admitted.runtime_backend) == (SEAT, BACKEND)
    assert admitted.runtime_target == {"pod_namespace": "swarm-pod-proof", "pod_name": "swarm-t1"}
    assert admitted.account == pod["metadata"]["labels"]["swarm.agentihooks.io/provider-account"] == ACCOUNT
    assert admitted.profile == profile
    assert pod["metadata"]["annotations"]["swarm.agentihooks.io/profile"] == "general"
    assert launches[0]["profile"] == "general"
    assert launches[0]["task_payload"]["profile"] == profile
    assert json.loads(pods.maps[0]["data"]["launch.json"])["profile"] == profile
    assert ("secrets", "swarm-claude-accounts") in pods.reads
    assert pods.nodes_read == 1
    assert lease.current(store, SLUG) is None


@pytest.fixture
def proof_controller(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_swarm_v2_deployed_runtime.SLUG", "proof-123abc-t1-1")
    store, pods = _store(), Pods("swarm-pod-proof")
    settings = _environ(tmp_path, store)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(deployed, "pod_api", lambda *_: pods)
    service = _cs().host(settings, store, "hive-fixture")
    try:
        yield store, pods, service
    finally:
        service.stop()


@pytest.mark.parametrize(
    "selected,lane,backend",
    [
        ("proof-123abc-t1-1", "eng", BACKEND),
        ("", "eng", "hive"),
        ("proof-123abc-t2-1", "eng", "hive"),
        ("proof-123abc-t1-1", "ci", "hive"),
        ("proof-123abc-t1-1", "plan", "hive"),
        ("proof-123abc-t1-1", "dispatch", "hive"),
    ],
)
def test_proof_engineer_placement_preserves_other_swarms_and_lanes(
    proof_controller, monkeypatch, selected, lane, backend
):
    from unittest.mock import Mock

    from scripts.swarm.runtime import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    store, pods, service = proof_controller
    monkeypatch.setenv("AGENTIHOOKS_CONTROL_KUBERNETES_PROOF", selected)
    fenced = loop.FencedRuntime(store, SLUG, service.controller.held, service.runtime, "distributed")
    hive = fenced.runtime.hive_launch
    hive.select = Mock(return_value=object())
    hive.spawn = Mock(return_value=Outcome("spawn", Status.OK, "hive", Placed("hive-pane", "claude", placement="hive")))
    task = {"id": "t1", "title": "Fixture", "seat": f"{lane}-1@{SLUG}", "profile": "engineer"}
    assert fenced.placement_refusal(lane, task) == ""
    hive.select.reset_mock()
    placed = fenced.spawn(store.config(SLUG), lane, store.next_name(SLUG, lane), task)
    assert placed.placement == backend
    if backend == BACKEND:
        hive.select.assert_not_called()
        hive.spawn.assert_not_called()
        assert len(pods.created) == 1
    else:
        hive.select.assert_called_once()
        hive.spawn.assert_called_once()
        assert pods.created == []


def test_proof_engineer_placement_keeps_workstation_and_disabled_backend_refusals(proof_controller, monkeypatch):
    store, pods, service = proof_controller
    monkeypatch.setenv("AGENTIHOOKS_CONTROL_KUBERNETES_PROOF", SLUG)
    fenced = loop.FencedRuntime(store, SLUG, service.controller.held, service.runtime, "distributed")
    task = {"id": "t1", "workstation": True}
    assert fenced.placement_refusal("eng", task) == loop.WORKSTATION.format(lane="eng")
    service.runtime.router.disabled = frozenset((BACKEND,))
    assert fenced.placement_refusal("eng", {"id": "t1"}) == loop.WORKSTATION.format(lane="eng")
    assert pods.created == []


def test_ordinary_swarm_cannot_opt_into_proof_placement(proof_controller, monkeypatch):
    store, _, service = proof_controller
    slug = "rig-grade-swarm"
    monkeypatch.setenv("AGENTIHOOKS_CONTROL_KUBERNETES_PROOF", slug)
    fenced = loop.FencedRuntime(store, slug, service.controller.held, service.runtime, "distributed")
    assert fenced.kubernetes_proof is False
    assert loop._hive_lane("eng", {}, fenced.kubernetes_proof) is True
    assert loop._hive_lane("master", {}, True) is True


def test_proof_engineer_worker_launch_records_a_real_checkpoint(proof_controller, tmp_path, monkeypatch):
    from scripts.swarm_v2 import supervision_protocol, supervision_runtime
    from scripts.swarm_v2.checkpoints import capture, runtime
    from scripts.swarm_v2.supervision import Launch
    from tests.test_swarm_v2_checkpoints_capture import portable

    store, pods, service = proof_controller
    monkeypatch.setenv("AGENTIHOOKS_CONTROL_KUBERNETES_PROOF", SLUG)
    fenced = loop.FencedRuntime(store, SLUG, service.controller.held, service.runtime, "distributed")
    task = {"id": "t1", "title": "Fixture", "seat": f"eng-1@{SLUG}", "profile": "engineer"}
    assert fenced.placement_refusal("eng", task) == ""
    placed = fenced.spawn(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
    assert placed.placement == BACKEND
    spec = json.loads(pods.maps[0]["data"]["launch.json"])
    execution = spec["registration"]["execution_id"]
    assert store.execution(SLUG, execution).runtime_backend == BACKEND
    attempt, _, environ, newest = portable.__wrapped__(tmp_path, monkeypatch)
    old_home = Path(environ["CLAUDE_CONFIG_DIR"])
    home = attempt / "homes" / "claude"
    home.parent.mkdir(parents=True)
    newest_relative = newest.relative_to(old_home)
    old_home.rename(home)
    environ["CLAUDE_CONFIG_DIR"] = str(home)
    newest = home / newest_relative
    spec.pop("checkout", None)
    (attempt / "launch.json").write_text(json.dumps(spec))
    record = json.loads((attempt / "execution.json").read_text())
    record["attempt"] = execution
    (attempt / "execution.json").write_text(json.dumps(record))
    owner = supervision_runtime.Supervisor(Launch.load(attempt, attempt / "launch.json"))
    owner.environment.update(environ)
    owner.root.mkdir(parents=True)
    monkeypatch.setattr(owner, "quiesce", lambda: True)
    monkeypatch.setattr(supervision_runtime.trees, "cleanup", lambda *_: None)

    elapsed = [0]
    monkeypatch.setattr(supervision_runtime.time, "monotonic", lambda: elapsed[0])

    def export():
        elapsed[0] += 1
        acknowledgement = capture.publish(attempt, owner.scope, environ, newest)
        supervision_protocol.write(owner.root / "exporter.checkpoint.json", {**owner.scope, **acknowledgement})

    monkeypatch.setattr(owner, "wait", export)
    result = owner.drain("termination")
    assert result["checkpoint_status"] == "complete"
    assert result["checkpoint_id"] == f"ckp-{owner.scope['incarnation']}"
    assert result["launch_record"]["checkpoint_acknowledgement"]["status"] == "complete"
    assert runtime.validate_export(attempt, result["checkpoint_id"], environ) is True


def _launch_record(workers, account):
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=API_URL)
    task = {
        "id": "t1",
        "seat": SEAT,
        "controller_epoch": 4,
        "execution_id": "exe-" + "1" * 32,
        "generation": 1,
        "endpoints": {"api_url": API_URL},
        "profile": "engineer",
        "account": account,
    }
    record = workers.launch(SpawnRequest(config, "eng", "engineer-1", task))
    return {**record, "execution_id": task["execution_id"], "generation": 1}


def test_the_worker_pod_admits_a_launch_account_the_shared_secret_holds_and_refuses_one_it_lacks(tmp_path, monkeypatch):
    pods = Pods("swarm-pod-proof", keys=("codex-fixtureatexample-com",))
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    workers = deployed.Workers.from_environ(_workers(tmp_path), SLUG)
    template = workers.transport({}, SLUG).template
    record = _launch_record(workers, ACCOUNT)

    with pytest.raises(PodSpecRefused) as refused:
        template.render(record)
    assert (str(refused.value), refused.value.reason) == (
        "launch provider_account is not an approved provider account",
        "account",
    )

    pods.keys = ("codex-fixtureatexample-com", ACCOUNT)
    rendered = template.render(record)
    assert rendered.pod["metadata"]["labels"]["swarm.agentihooks.io/provider-account"] == ACCOUNT
    assert pods.reads == [("secrets", "swarm-claude-accounts")] * 2


def test_the_deployed_codex_worker_reads_its_access_index_and_own_credential(tmp_path, monkeypatch):
    pods = Pods("swarm-pod-proof", keys=("AH_CX_TOKEN_alpha_personal", ACCOUNT))
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    workers = deployed.Workers.from_environ(_workers(tmp_path), SLUG)
    template = workers.transport({}, SLUG).template
    record = _launch_record(workers, ACCOUNT)
    record.update(harness="codex", provider_account="alpha-personal")
    rendered = template.render(record)
    entries = [entry for entry in rendered.pod["spec"]["containers"][0]["env"] if "valueFrom" in entry]
    assert entries == [
        {
            "name": "AH_CX_TOKEN_alpha_personal",
            "valueFrom": {"secretKeyRef": {"name": "swarm-codex-creds", "key": "AH_CX_TOKEN_alpha_personal"}},
        }
    ]
    assert pods.reads == [("secrets", "swarm-codex-accounts")]
    pods.keys = (ACCOUNT, "AH_CX_REFRESH_alpha_personal")
    with pytest.raises(PodSpecRefused, match="not an approved provider account"):
        template.render(record)
    assert pods.reads == [("secrets", "swarm-codex-accounts")] * 2


def test_a_tick_launch_with_no_key_in_the_shared_secret_is_refused_before_any_pod(tmp_path, monkeypatch):
    store = _store()
    pods = Pods("swarm-pod-proof", keys=())
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        with pytest.raises(SpawnError) as refused:
            service.runtime.spawn(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
    finally:
        service.stop()

    assert str(refused.value) == "no approved account has quota and a free launch slot"
    assert pods.created == []
    assert store.executions(SLUG, SEAT) == []


def test_a_tick_launch_skips_an_approved_key_whose_fleet_slots_are_full(tmp_path, monkeypatch):
    store = _store()
    other = "codex-fixtureatexample-com"
    pods = Pods("swarm-pod-proof", keys=(ACCOUNT, other))
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    for seat in ("eng-8@elsewhere", "eng-9@elsewhere"):
        held = Slot(ACCOUNT, seat, "exe-" + "9" * 32, 1, RESERVED, expires_ms=10**15)
        store.redis.hset(account_key(ACCOUNT), seat, encode(held))
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        service.runtime.spawn(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
    finally:
        service.stop()

    [pod] = pods.created
    assert pod["metadata"]["labels"]["swarm.agentihooks.io/provider-account"] == other


def test_a_tick_launch_takes_the_account_the_capacity_plan_placed_its_task_on(tmp_path, monkeypatch):
    store = _store()
    other = "codex-fixtureatexample-com"
    pods = Pods("swarm-pod-proof", keys=(ACCOUNT, other))
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        herdr = service.runtime.herdr_runtime
        herdr._quota_tasks, herdr._quota_task_accounts = {"t1": "claude"}, {"t1": other}
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        service.runtime.spawn(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
    finally:
        service.stop()

    [pod] = pods.created
    assert pod["metadata"]["labels"]["swarm.agentihooks.io/provider-account"] == other


def test_a_tick_launch_skips_an_approved_key_whose_newest_fleet_quota_is_rejected(tmp_path, monkeypatch):
    store = _store()
    other = "codex-fixtureatexample-com"
    pods = Pods("swarm-pod-proof", keys=(ACCOUNT, other))
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    seen = quota.Observation(ACCOUNT, "claude", "exe-" + "9" * 32, 1, lease.now_ms(store), "rejected", 1.0, 1.0)
    store.redis.hset(quota.latest_key("claude"), ACCOUNT, quota.encode(seen))
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        service.runtime.spawn(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
    finally:
        service.stop()

    [pod] = pods.created
    assert pod["metadata"]["labels"]["swarm.agentihooks.io/provider-account"] == other


def test_a_controller_with_no_local_accounts_sizes_engineer_and_ci_lanes_from_fleet_accounts(tmp_path, monkeypatch):
    def local(*args, **kwargs):
        raise AssertionError("the deployed controller read local accounts")

    monkeypatch.setattr(capacity, "accounts", local)
    monkeypatch.setattr(account_sessions, "sessions_by_account", local)
    store = _store()
    other = "codex-fixtureatexample-com"
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods("swarm-pod-proof", keys=(ACCOUNT, other)))
    held = Slot(ACCOUNT, "eng-9@elsewhere", "exe-" + "9" * 32, 1, RESERVED, expires_ms=10**15)
    store.redis.hset(account_key(ACCOUNT), held.holder, encode(held))
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        config = replace(store.config(SLUG), max_eng=2, max_ci=1)
        service.runtime.herdr_runtime.host = lambda: None
        decision = service.runtime.quota_capacity(config, [], 1_800_000_000, {"eng": 2, "ci": 1, "plan": 0})
    finally:
        service.stop()

    assert decision["effective"] == {"eng": 1, "ci": 1, "plan": 0}
    assert decision["placeable"] == {"claude": 3, "codex": 0}
    assert [(row["name"], row["sessions"], row["cap"]) for row in decision["accounts"]] == [
        (ACCOUNT, 1, 2),
        (other, 0, 2),
    ]


class PoolPods(Pods):
    def __init__(self, namespace, pods):
        super().__init__(namespace)
        self.pods, self.selectors = pods, []

    def list_pods(self, selector):
        self.selectors.append(selector)
        return self.pods


def _worker_pod(phase):
    agent = {"name": "agent", "resources": {"requests": {"cpu": "2", "memory": "4Gi"}}}
    bundle = {"name": "bundle", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}
    return {"spec": {"containers": [agent], "initContainers": [bundle]}, "status": {"phase": phase}}


def test_the_deployed_controller_sizes_spawn_room_from_worker_namespace_usage_and_never_reads_its_own_node(
    tmp_path, monkeypatch
):
    node_reads = []

    def own_node(*args, **kwargs):
        node_reads.append(args)
        return host_budget.HostSample(load1=9.0, cpus=1, available_mb=10, agents=5)

    monkeypatch.setattr(host_budget, "read_host", own_node)
    pods = PoolPods("swarm-pod-proof", [_worker_pod("Running"), _worker_pod("Pending"), _worker_pod("Succeeded")])
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    pool = {"pods": 8, "cpu_millis": 18000, "memory_mib": 30000}
    policy = tmp_path / "pool-policy.json"
    policy.write_text(json.dumps({**json.loads(_policy(tmp_path).read_text()), "worker_pool": pool}))
    store = _store()
    service = _cs().host(_environ(tmp_path, store, **{deployed.POLICY_ENV: str(policy)}), store, "hive-fixture")
    try:
        decision = service.runtime.quota_capacity(store.config(SLUG), [], 1_800_000_000, {"eng": 1, "ci": 0, "plan": 0})
    finally:
        service.stop()

    assert node_reads == []
    assert "" in pods.selectors
    assert {name: decision["host"][name] for name in ("room", "reason", "limit", "held")} == {
        "room": 5,
        "reason": "worker pool: 2 of 8 Pods, 4000m of 18000m CPU and 8192 MiB of 30000 MiB memory requested; "
        "5 more fit at 2000m and 4096 MiB each",
        "limit": "memory",
        "held": False,
    }


def test_a_deployed_controller_whose_policy_declares_no_pool_passes_spawns_without_reading_its_own_node(
    tmp_path, monkeypatch
):
    def own_node(*args, **kwargs):
        raise AssertionError("the deployed controller read its own node")

    monkeypatch.setattr(host_budget, "read_host", own_node)
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: PoolPods("swarm-pod-proof", []))
    store = _store()
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        decision = service.runtime.quota_capacity(store.config(SLUG), [], 1_800_000_000, {"eng": 1, "ci": 0, "plan": 0})
    finally:
        service.stop()

    assert (decision["host"]["room"], decision["host"]["reason"]) == (
        None,
        "worker namespace quota sets no Pod, CPU or memory limit; "
        "worker pool undeclared: the Pod policy names no worker_pool, so spawns pass",
    )


class QuotaPods(PoolPods):
    def list_quotas(self):
        hard = {"pods": "8", "requests.cpu": "18", "requests.memory": "34Gi"}
        return [{"metadata": {"name": "swarm-worker-execution"}, "status": {"hard": hard, "used": {"pods": "6"}}}]


def test_the_deployed_controller_sizes_spawn_room_from_the_worker_namespace_quota_with_no_pool_declared(
    tmp_path, monkeypatch
):
    def own_node(*args, **kwargs):
        raise AssertionError("the deployed controller read its own node")

    monkeypatch.setattr(host_budget, "read_host", own_node)
    pods = QuotaPods("swarm-pod-proof", [])
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    store = _store()
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        decision = service.runtime.quota_capacity(store.config(SLUG), [], 1_800_000_000, {"eng": 1, "ci": 0, "plan": 0})
    finally:
        service.stop()

    assert pods.selectors == []
    assert (decision["host"]["room"], decision["host"]["limit"], decision["host"]["reason"]) == (
        2,
        "quota pods",
        "worker namespace quota leaves 2 Pods, 18000m CPU and 34816 MiB memory; 2 more fit at 2000m and 4096 MiB each",
    )


def _requesting(tmp_path, cpu_millis=1000):
    doc = json.loads(_policy(tmp_path).read_text())
    doc["profiles"]["general"]["requests"] = {"cpu_millis": cpu_millis}
    path = tmp_path / "requesting-policy.json"
    path.write_text(json.dumps(doc))
    return path


def test_the_quota_room_counts_each_launch_at_its_profile_cpu_request(tmp_path, monkeypatch):
    monkeypatch.setattr(host_budget, "read_host", lambda *args, **kwargs: None)
    pods = QuotaPods("swarm-pod-proof", [])
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    store = _store()
    environ = _environ(tmp_path, store, **{deployed.POLICY_ENV: str(_requesting(tmp_path))})
    service = _cs().host(environ, store, "hive-fixture")
    try:
        decision = service.runtime.quota_capacity(store.config(SLUG), [], 1_800_000_000, {"eng": 1, "ci": 0, "plan": 0})
    finally:
        service.stop()

    assert decision["host"]["reason"] == (
        "worker namespace quota leaves 2 Pods, 18000m CPU and 34816 MiB memory; 2 more fit at 1000m and 4096 MiB each"
    )


def test_the_worker_pool_room_counts_each_launch_at_its_profile_cpu_request(tmp_path, monkeypatch):
    monkeypatch.setattr(host_budget, "read_host", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: PoolPods("swarm-pod-proof", []))
    doc = json.loads(_requesting(tmp_path).read_text())
    doc["worker_pool"] = {"pods": 8, "cpu_millis": 3000, "memory_mib": 30000}
    policy = tmp_path / "pool-policy.json"
    policy.write_text(json.dumps(doc))
    store = _store()
    service = _cs().host(_environ(tmp_path, store, **{deployed.POLICY_ENV: str(policy)}), store, "hive-fixture")
    try:
        decision = service.runtime.quota_capacity(store.config(SLUG), [], 1_800_000_000, {"eng": 1, "ci": 0, "plan": 0})
    finally:
        service.stop()

    assert (decision["host"]["room"], decision["host"]["limit"]) == (3, "cpu")


def test_a_pod_policy_that_names_a_provider_account_is_refused(tmp_path):
    path = tmp_path / "named-pod-policy.json"
    path.write_text(json.dumps({**json.loads(_policy(tmp_path).read_text()), "provider_accounts": ["a@example.com"]}))

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.POLICY_ENV: str(path)}), SLUG)

    assert str(refused.value) == (
        "the Pod policy must name no provider account: the approved accounts are the keys of Secret swarm-claude-accounts"
    )


def test_the_worker_settings_take_no_launch_account(tmp_path):
    workers = deployed.Workers.from_environ(_workers(tmp_path), SLUG)
    assert workers.terms.account == ""
    assert "AGENTIHOOKS_LAUNCH_ACCOUNT" not in deployed.REQUIRED


def _task(execution="1"):
    return {
        "id": "t1",
        "seat": SEAT,
        "controller_epoch": 4,
        "execution_id": "exe-" + execution * 32,
        "generation": 1,
        "account": ACCOUNT,
        "profile": "engineer",
        "endpoints": {"api_url": API_URL},
    }


def test_each_launch_resolves_the_worker_image_tag_to_its_current_digest(tmp_path, registry):
    asked, digests = registry
    moved = "sha256:" + "5c" * 32
    digests.append(moved)
    workers = deployed.Workers.from_environ(_workers(tmp_path, **{deployed.IMAGE_TAG_ENV: "sha-e04f98f3f"}), SLUG)
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=API_URL)

    first = workers.launch(SpawnRequest(config, "eng", "engineer-1", _task("1")))
    second = workers.launch(SpawnRequest(config, "eng", "engineer-2", _task("2")))

    assert (first["image_digest"], second["image_digest"]) == (IMAGE, moved)
    assert asked == [(REPOSITORY, "sha-e04f98f3f"), (REPOSITORY, "sha-e04f98f3f")]


@pytest.mark.parametrize("tag", ["sha256:" + "4b" * 32, "dev@sha256:" + "4b" * 32, "-dev"])
def test_a_worker_image_tag_that_is_a_digest_or_malformed_is_refused(tmp_path, tag):
    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.IMAGE_TAG_ENV: tag}), SLUG)

    assert str(refused.value) == f"{deployed.IMAGE_TAG_ENV} must be an image tag, never a digest"


def test_a_worker_image_the_registry_cannot_resolve_refuses_the_spawn_before_any_create(tmp_path, monkeypatch):
    def unresolved(repository, tag):
        raise image_tag.ImageUnresolved(f"the registry answered 404 for {repository}:{tag}")

    monkeypatch.setattr(image_tag, "resolve", unresolved)
    workers, created = deployed.Workers.from_environ(_workers(tmp_path), SLUG), []
    runtime = KubernetesRuntime(created.append, workers.launch)
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=API_URL)

    outcome = runtime.spawn(SpawnRequest(config, "eng", "engineer-1", _task()))

    assert (outcome.status.value, outcome.backend, outcome.detail) == (
        "refused",
        BACKEND,
        f"the registry answered 404 for {REPOSITORY}:dev",
    )
    assert created == []


def test_the_start_logs_that_it_built_the_kubernetes_runtime(tmp_path, monkeypatch, capsys):
    store = _store()
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods(namespace))

    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        assert capsys.readouterr().out == (
            f"controller: built the Kubernetes runtime for {SLUG}: workers in swarm-pod-proof "
            f"run {REPOSITORY}:dev with profile general\n"
        )
    finally:
        service.stop()


def test_the_started_recovery_pass_hands_the_proof_chooser_the_controller_environment(tmp_path, monkeypatch):
    store, chosen = _store(), []
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods(namespace))
    monkeypatch.setattr(
        deployed.proof_swarm, "choose_fresh", lambda environ, recovery: chosen.append((environ, recovery))
    )
    environ = _environ(tmp_path, store)

    service = _cs().host(environ, store, "hive-fixture")
    try:
        service.recover.choose()
        ((handed, recovery),) = chosen
        assert (handed, recovery.slug) == (environ, SLUG)
    finally:
        service.stop()


def test_deployed_checkpoint_reader_maps_historical_claim_and_execution_generations(tmp_path, monkeypatch):
    from scripts.swarm_v2.auth_context import Registration

    store = _store()
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods(namespace))
    environ = _environ(tmp_path, store, **{"AGENTIHOOKS_CHECKPOINT_ROOT": str(tmp_path / "durable")})
    service = _cs().host(environ, store, "hive-fixture")
    registration = Registration(
        "g", SLUG, "e1", 2, SEAT, "t1", ACCOUNT, "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    calls = []

    def registered(slug, execution):
        calls.append(("registration", slug, execution))
        return registration if slug == SLUG and execution == "e1" else None

    def journal(task):
        calls.append(("journal", task))
        return [{"claim": {"execution_id": "e1", "execution_generation": 2, "generation": 9}}] if task == "t1" else []

    try:
        monkeypatch.setattr(service.grants, "registration", registered)
        monkeypatch.setattr(service.tasks, "journal", journal)
        reader = service.recover.recovery.checkpoints
        assert reader.root == tmp_path / "durable"
        assert reader.compatibility == "dev"
        validator = reader.validator("e1")
        assert validator.store.scope.generation == 9
        assert validator.store.scope.execution_id == "e1"
        assert validator.execution_generation == 2
        assert reader.validator("unknown") is None
        assert calls == [
            ("registration", SLUG, "e1"),
            ("journal", "t1"),
            ("registration", SLUG, "e1"),
            ("registration", SLUG, "unknown"),
        ]
    finally:
        service.stop()


def test_the_runtime_line_is_flushed_so_the_container_log_shows_it_at_once(tmp_path, monkeypatch):
    store, printed = _store(), []
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods(namespace))
    monkeypatch.setattr(_cs(), "print", lambda *args, **kwargs: printed.append(kwargs), raising=False)

    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        assert printed == [{"flush": True}]
    finally:
        service.stop()


def test_a_start_without_an_api_address_logs_no_kubernetes_runtime(tmp_path, capsys):
    store = _store()
    environ = _environ(tmp_path, store, **{deployed.API_URL_ENV: None, deployed.POLICY_ENV: None})

    service = _cs().host(environ, store, "hive-fixture")
    try:
        assert capsys.readouterr().out == ""
    finally:
        service.stop()


def test_the_deployed_controller_adds_each_remote_pod_reading_to_its_observations(tmp_path, monkeypatch):
    store, pods, listed = _store(), Pods("swarm-pod-proof"), []

    def running(selector):
        name = pod_name(selector.rsplit("=", 1)[1])
        return [{"metadata": {"name": name, "uid": "uid-1"}, "status": {"phase": "Running"}}]

    monkeypatch.setattr(pods, "list_pods", lambda selector: listed.append(selector) or running(selector))
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        target = {"pod_namespace": "swarm-pod-proof", "pod_name": "worker-t1"}
        record = AgentRecord(
            store.next_name(SLUG, "eng"), "eng", "t1", seat=SEAT, runtime_backend=BACKEND, runtime_target=target
        )
        agent = service.controller.admit(record, "")
        service.observe(60.0)
    finally:
        service.stop()

    assert observe.stored(store, SLUG, agent.execution_id).sources == {
        "kubernetes": {"reading": "ok", "observed_at": 60.0, "value": "Running"},
    }
    assert f"{OWNER_LABEL}={owner_for(SLUG)},{EXECUTION_LABEL}={agent.execution_id}" in listed


def test_the_launch_record_names_the_admitted_execution_and_the_worker_settings(tmp_path, registry):
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
        "profile": "engineer",
        "account": ACCOUNT,
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
        "task_payload": {
            "task_id": "t1",
            "lane": "eng",
            "name": "engineer-1",
            "profile": "engineer",
            "endpoints": {"api_url": API_URL},
        },
    }
    assert registry[0] == [(REPOSITORY, "dev")]


def test_a_launch_requests_the_profile_cpu_request_and_its_whole_memory_limit(tmp_path):
    workers = deployed.Workers.from_environ(
        _workers(tmp_path, **{deployed.POLICY_ENV: str(_requesting(tmp_path, 1500))}), SLUG
    )
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=API_URL)
    task = {
        "id": "t1",
        "seat": SEAT,
        "controller_epoch": 4,
        "execution_id": "exe-" + "1" * 32,
        "generation": 1,
        "endpoints": {"api_url": API_URL},
        "account": ACCOUNT,
        "profile": "engineer",
    }
    record = workers.launch(SpawnRequest(config, "eng", "engineer-1", task))

    assert (record["cpu_millis"], record["memory_mib"]) == (1500, 4096)


def test_a_replacement_launch_record_carries_the_checkpoint_it_resumes_from(tmp_path, registry):
    workers = deployed.Workers.from_environ(_workers(tmp_path), SLUG)
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=API_URL)
    task = {**_task(), "checkpoint": "ckpt-one-2"}

    record = workers.launch(SpawnRequest(config, "eng", "engineer-1", task))

    assert record["task_payload"] == {
        "task_id": "t1",
        "lane": "eng",
        "name": "engineer-1",
        "profile": "engineer",
        "endpoints": {"api_url": API_URL},
        "checkpoint": "ckpt-one-2",
    }
    fresh = workers.launch(SpawnRequest(config, "eng", "engineer-1", {**task, "checkpoint": ""}))
    assert "checkpoint" not in fresh["task_payload"]


def test_a_pod_policy_that_delivers_a_bundle_admits_bundle_profiles(tmp_path):
    path = tmp_path / "bundled-pod-policy.json"
    bundle = {"image_repository": "ghcr.io/fixture/agentihooks-bundle", "tag": "dev", "pull_access": "bundle-pull"}
    path.write_text(json.dumps({**json.loads(_policy(tmp_path).read_text()), "bundle": bundle}))

    workers = deployed.Workers.from_environ(_workers(tmp_path, **{deployed.POLICY_ENV: str(path)}), SLUG)

    assert workers.terms.bundled is True
    assert deployed.Workers.from_environ(_workers(tmp_path), SLUG).terms.bundled is False


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
    environ = _environ(tmp_path, store, **{deployed.IMAGE_TAG_ENV: None, deployed.BRAIN_ENV: None})

    with pytest.raises(SwarmError) as refused:
        _cs().host(environ, store, "hive-fixture")

    assert str(refused.value) == f"the Kubernetes runtime needs {deployed.IMAGE_TAG_ENV}, {deployed.BRAIN_ENV}"
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


def test_an_agent_profile_without_an_approved_resource_class_is_refused(tmp_path):

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.PROFILE_ENV: "frontend"}), SLUG)

    assert str(refused.value) == "the Pod policy has no frontend profile"


def test_one_pod_policy_serves_two_swarms_each_rendering_its_own_owner(tmp_path, monkeypatch):
    pods = Pods("swarm-pod-proof", keys=(ACCOUNT,))
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    owners = {}
    for slug in (SLUG, "other-swarm"):
        workers = deployed.Workers.from_environ(_workers(tmp_path), slug)
        transport = workers.transport({}, slug)
        rendered = transport.template.render(_launch_record(workers, ACCOUNT))
        owners[slug] = (transport.owner, rendered.pod["metadata"]["labels"][OWNER_LABEL])
    assert owners == {
        SLUG: (owner_for(SLUG), owner_for(SLUG)),
        "other-swarm": ("agentihooks-swarm-other-swarm", "agentihooks-swarm-other-swarm"),
    }


def test_a_pod_policy_naming_an_owner_is_refused(tmp_path):
    other = tmp_path / "other-policy.json"
    other.write_text(json.dumps({**json.loads(POLICY.read_text()), "owner": owner_for(SLUG)}))
    environ = _workers(tmp_path, **{deployed.POLICY_ENV: str(other)})

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(environ, SLUG)

    with pytest.raises(PodSpecRefused) as expected:
        load_policy(other)
    assert str(refused.value) == str(expected.value)


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
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        request = SpawnRequest(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
        target = runtime.launch.keywords["target"](request)
        agent = AgentRecord(
            request.name, "eng", "t1", seat=SEAT, runtime_backend=BACKEND, runtime_target=target, profile="engineer"
        )
        launch = launcher.spawn(request, agent, replace(runtime.launch.keywords["terms"], account=ACCOUNT), "", "")
        claims = service.grants.verify(SLUG, pods.maps[0]["data"]["launch-grant"])
    finally:
        service.stop()

    assert launch.hand.handed is True
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
            "data": {"launch-grant": launch.grant, "launch.json": pods.maps[0]["data"]["launch.json"]},
        }
    ]
    assert claims.execution_id == execution
    assert json.loads(pods.maps[0]["data"]["launch.json"]) == {
        "schema_version": 1,
        "execution_id": execution,
        "generation": launch.agent.generation,
        "authority": {
            "execution_id": execution,
            "generation": launch.agent.generation,
            "task_id": "t1",
            "seat_id": SEAT,
            "swarm_id": SLUG,
            "grant_id": claims.grant_id,
        },
        "registration": asdict(claims),
        "image_digest": pod["spec"]["containers"][0]["image"].split("@", 1)[1],
        "harness": "claude",
        "agent": ["claude"],
        "exporter": ["python", "-m", "scripts.swarm_v2.session_export"],
        "control_url": API_URL,
        "profile": "engineer",
        "startup_seconds": 10,
        "quiesce_seconds": 10,
        "checkpoint_seconds": 40,
        "kill_seconds": 2,
        "checkpoint_acknowledgement": {"status": "pending"},
        "checkout": {"project": PROJECT, "agent": request.name},
    }


@pytest.mark.parametrize(
    ("storage", "remote"),
    [
        ({}, False),
        ({"AGENTIHOOKS_CHECKPOINT_ROOT": "/fixture/checkpoints"}, False),
        ({"AGENTIHOOKS_CHECKPOINT_BUCKET": "fixture-checkpoints"}, True),
        ({"AGENTIHOOKS_CHECKPOINT_BUCKET": ""}, False),
    ],
)
def test_worker_remote_transport_requires_the_controller_bucket(tmp_path, monkeypatch, storage, remote):
    store = _store()
    pods = Pods("swarm-pod-proof")
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    environ = _workers(tmp_path, **storage)
    workers = deployed.Workers.from_environ(environ, SLUG)
    grants = workers.grants(environ, SLUG, lambda execution: None, store)
    assert grants.supervision.checkpoint_remote is remote


def test_the_exporter_worker_setting_reaches_the_launch_record(tmp_path, monkeypatch):
    store = _store()
    pods = Pods("swarm-pod-proof")
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    environ = _environ(tmp_path, store, **{deployed.EXPORTER_ENV: '["python", "-m", "exporter"]'})
    service = _cs().host(environ, store, "hive-fixture")
    try:
        runtime = service.runtime
        launcher = runtime.launch.func.__self__
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        request = SpawnRequest(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
        target = runtime.launch.keywords["target"](request)
        agent = AgentRecord(request.name, "eng", "t1", seat=SEAT, runtime_backend=BACKEND, runtime_target=target)
        launch = launcher.spawn(request, agent, replace(runtime.launch.keywords["terms"], account=ACCOUNT), "", "")
    finally:
        service.stop()

    assert launch.hand.handed is True
    assert json.loads(pods.maps[0]["data"]["launch.json"])["exporter"] == ["python", "-m", "exporter"]


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_exporter_setting_names_the_image_session_exporter(tmp_path, value):
    workers = deployed.Workers.from_environ(_workers(tmp_path, **{deployed.EXPORTER_ENV: value}), SLUG)

    assert workers.exporter == ("python", "-m", "scripts.swarm_v2.session_export")


def test_an_exporter_setting_becomes_its_command_words(tmp_path):
    environ = _workers(tmp_path, **{deployed.EXPORTER_ENV: '["python", "-m", "exporter"]'})

    assert deployed.Workers.from_environ(environ, SLUG).exporter == ("python", "-m", "exporter")


@pytest.mark.parametrize(
    "value", ["python -m exporter", "[]", '[""]', '["ok", 1]', '"exporter"', '{"a": 1}', '["a\\u0000b"]']
)
def test_a_malformed_exporter_setting_is_refused(tmp_path, value):

    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.EXPORTER_ENV: value}), SLUG)

    assert str(refused.value) == f"{deployed.EXPORTER_ENV} must be a JSON list of command words"


TERMINAL_CA = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIALn9mCQtGSdrcOS/cqqJSfiydwdYjJBgSb0Ofwx+lv6"
TERMINAL = json.dumps(
    {"clients": {"namespace": "agentihooks-swarm", "labels": {"app": "swarm-operator"}}, "user_ca": TERMINAL_CA}
)


def _spawn(tmp_path, monkeypatch, **changes):
    store = _store()
    pods = Pods("swarm-pod-proof")
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    service = _cs().host(_environ(tmp_path, store, **changes), store, "hive-fixture")
    try:
        runtime = service.runtime
        launcher = runtime.launch.func.__self__
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        request = SpawnRequest(store.config(SLUG), "eng", store.next_name(SLUG, "eng"), task)
        target = runtime.launch.keywords["target"](request)
        agent = AgentRecord(request.name, "eng", "t1", seat=SEAT, runtime_backend=BACKEND, runtime_target=target)
        launch = launcher.spawn(request, agent, replace(runtime.launch.keywords["terms"], account=ACCOUNT), "", "")
        claims = service.grants.verify(SLUG, pods.maps[0]["data"]["launch-grant"])
    finally:
        service.stop()
    return store, pods, launch, claims


def test_terminal_exposure_mounts_the_pod_and_hands_it_its_secret_service_and_policy(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization

    from scripts.swarm_v2.herdr import endpoints
    from scripts.swarm_v2.kubernetes.terminal import HostKeys

    store, pods, launch, claims = _spawn(tmp_path, monkeypatch, AGENTIHOOKS_WORKER_TERMINAL=TERMINAL)

    assert launch.hand.handed is True
    execution = launch.agent.execution_id
    [pod] = pods.created
    container = pod["spec"]["containers"][0]
    assert {"name": "terminal", "mountPath": endpoints.MATERIAL_DIR, "readOnly": True} in container["volumeMounts"]
    assert [volume["secret"]["secretName"] for volume in pod["spec"]["volumes"] if volume["name"] == "terminal"] == [
        f"swarm-{execution}-terminal"
    ]
    assert [(body["kind"], body["metadata"]["name"]) for body in pods.objects] == [
        ("Secret", f"swarm-{execution}-terminal"),
        ("Service", f"swarm-{execution}-terminal"),
        ("NetworkPolicy", f"swarm-{execution}-terminal"),
    ]
    secret, _, policy = pods.objects
    peer = policy["spec"]["ingress"][0]["from"][0]
    assert peer["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "agentihooks-swarm"}}
    assert peer["podSelector"] == {"matchLabels": {"app": "swarm-operator"}}
    assert endpoints.base64.b64decode(secret["data"][endpoints.USER_CA]).decode() == f"{TERMINAL_CA}\n"
    private = serialization.load_ssh_private_key(endpoints.base64.b64decode(secret["data"][endpoints.HOST_KEY]), None)
    launched = HostKeys(store).launched(SLUG, execution)
    assert (launched.generation, launched.host_public) == (claims.generation, endpoints._public(private.public_key()))


def test_terminal_exposure_off_creates_no_terminal_object_or_mount(tmp_path, monkeypatch):
    _, pods, launch, _ = _spawn(tmp_path, monkeypatch)

    assert launch.hand.handed is True
    [pod] = pods.created
    assert pods.objects == []
    assert all(volume["name"] != "terminal" for volume in pod["spec"]["volumes"])
    assert "ports" not in pod["spec"]["containers"][0]


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_terminal_setting_turns_exposure_off(tmp_path, value):
    workers = deployed.Workers.from_environ(_workers(tmp_path, **{deployed.TERMINAL_ENV: value}), SLUG)

    assert workers.terminal is None


def test_a_terminal_setting_names_its_clients_and_user_ca(tmp_path):
    from scripts.swarm_v2.herdr.endpoints import Clients
    from scripts.swarm_v2.kubernetes.terminal import Exposure

    workers = deployed.Workers.from_environ(_workers(tmp_path, **{deployed.TERMINAL_ENV: TERMINAL}), SLUG)

    assert workers.terminal == Exposure(Clients("agentihooks-swarm", {"app": "swarm-operator"}), TERMINAL_CA)


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        '{"user_ca": "' + TERMINAL_CA + '"}',
        '{"clients": {"namespace": "agentihooks-swarm", "labels": {"app": "x"}}}',
        '{"clients": [], "user_ca": "' + TERMINAL_CA + '"}',
        '{"clients": {"namespace": "Bad_Name", "labels": {"app": "x"}}, "user_ca": "' + TERMINAL_CA + '"}',
        '{"clients": {"namespace": "agentihooks-swarm", "labels": {}}, "user_ca": "' + TERMINAL_CA + '"}',
        '{"clients": {"namespace": "agentihooks-swarm", "labels": ["x"]}, "user_ca": "' + TERMINAL_CA + '"}',
        '{"clients": {"namespace": "agentihooks-swarm", "labels": {"app": "x"}}, "user_ca": "not a key"}',
    ],
)
def test_a_malformed_terminal_setting_is_refused(tmp_path, value):
    with pytest.raises(SwarmError) as refused:
        deployed.Workers.from_environ(_workers(tmp_path, **{deployed.TERMINAL_ENV: value}), SLUG)

    assert (
        str(refused.value)
        == "AGENTIHOOKS_WORKER_TERMINAL must be a JSON object naming the terminal clients and user CA"
    )


class HandPods(Pods):
    def __init__(self, namespace, refuse, lose):
        super().__init__(namespace)
        self.refuse, self.lose, self.deleted = refuse, lose, []

    def create_pod(self, body):
        self.created.append(body)
        return Pods.read_pod(self, body["metadata"]["name"])

    def read_pod(self, name):
        return None if self.lose else super().read_pod(name)

    def create_config_map(self, body):
        if self.refuse:
            raise ApiRefused(403, "Forbidden")
        return super().create_config_map(body)

    def delete(self, kind, name, uid):
        self.deleted.append((kind, name, uid))
        return True


@pytest.mark.parametrize(
    ("refuse", "lose", "hand"),
    [
        (False, False, {"handed": True, "reason": "handed", "removed": False}),
        (False, True, {"handed": False, "reason": "pod_missing", "removed": False}),
        (True, False, {"handed": False, "reason": "config_map_refused", "removed": True}),
    ],
    ids=["handed", "missing", "refused"],
)
def test_a_tick_launch_records_whether_its_grant_reached_the_pod(tmp_path, monkeypatch, refuse, lose, hand):
    store = _store()
    pods = HandPods("swarm-pod-proof", refuse, lose)
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    service = _cs().host(_environ(tmp_path, store), store, "hive-fixture")
    try:
        task = {"id": "t1", "title": "Fixture", "seat": SEAT, "controller_epoch": service.controller.held.epoch}
        name = store.next_name(SLUG, "eng")
        try:
            placed, failure = service.runtime.spawn(store.config(SLUG), "eng", name, task), None
        except SpawnError as error:
            placed, failure = None, error
    finally:
        service.stop()

    [pod] = pods.created
    execution = pod["metadata"]["labels"][EXECUTION_LABEL]
    hands = store.redis.hgetall(store.key(SLUG, "launch-grant-hands"))
    assert {key: json.loads(raw) for key, raw in hands.items()} == {execution: hand}
    assert pods.deleted == ([("pods", f"swarm-{execution}", "uid-1")] if refuse else [])
    if hand["handed"]:
        assert (placed.placement, failure) == (BACKEND, None)
    else:
        assert placed is None
        assert (str(failure), failure.status) == (f"launch grant not handed: {hand['reason']}", "refused")


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


def _anton_client(tmp_path, monkeypatch, store, **changes):
    key = tmp_path / "terminal-ca"
    key.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        )
    )
    herdr, environments = sv2_hdr03_cases.Herdr([sv2_hdr03_cases.fixture()["manual"]], set()), []
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: Pods(namespace))
    monkeypatch.setattr(_cs(), "runner", lambda environment: environments.append(environment) or herdr)
    settings = {deployed.TERMINAL_ENV: TERMINAL, operator_terminal.CA_KEY_ENV: str(key), **changes}
    return _cs().host(_environ(tmp_path, store, **settings), store, "hive-fixture"), herdr, environments


def test_a_controller_exposing_terminals_reconciles_the_anton_client_at_start(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr.upkeep import Upkeep

    service, herdr, environments = _anton_client(tmp_path, monkeypatch, _store())
    try:
        assert isinstance(service.controller.terminals, Upkeep)
        assert service.controller.terminals.terminal.namespace == "swarm-pod-proof"
        assert herdr.commands == [["herdr", "machine", "list", "--json"]]
        assert [environment["HOME"] for environment in environments] == [str(Path.home())]
        assert service.controller.herdr_registration_drift() == 0
    finally:
        service.stop()


def test_a_controller_with_the_jump_state_lists_its_worker_terminals_there(tmp_path, monkeypatch):
    from scripts.swarm_v2.kubernetes import jump

    state = tmp_path / "jump"
    state.mkdir()
    service, _, _ = _anton_client(tmp_path, monkeypatch, _store(), **{jump.STATE_ENV: str(state)})
    try:
        assert isinstance(service.controller.jump, jump.Routes)
        assert (service.controller.jump.state, service.controller.jump.namespace) == (state, "swarm-pod-proof")
        assert (state / "principals").read_text() == ""
    finally:
        service.stop()


def test_a_controller_without_the_jump_state_lists_no_jump_route(tmp_path, monkeypatch):
    service, _, _ = _anton_client(tmp_path, monkeypatch, _store())
    try:
        assert service.controller.jump is None
    finally:
        service.stop()


def test_switched_off_the_controller_runs_no_herdr_command_and_reads_no_ca_key(tmp_path, monkeypatch):
    off = {"AGENTIHOOKS_HERDR_REGISTRATIONS": "off", operator_terminal.CA_KEY_ENV: str(tmp_path / "missing")}
    service, herdr, environments = _anton_client(tmp_path, monkeypatch, _store(), **off)
    try:
        assert service.controller.terminals is None
        assert service.tick()
        assert service.controller.herdr_registration_drift() is None
        assert herdr.commands == []
        assert herdr.machines == [sv2_hdr03_cases.fixture()["manual"]]
        assert environments == []
    finally:
        service.stop()


def test_a_controller_without_the_terminal_ca_key_says_it_keeps_no_client(tmp_path, monkeypatch, capsys):
    service, herdr, environments = _anton_client(
        tmp_path, monkeypatch, _store(), **{operator_terminal.CA_KEY_ENV: None}
    )
    try:
        assert service.controller.terminals is None
        assert service.controller.herdr_registration_drift() is None
        assert herdr.commands == []
        assert environments == []
        assert capsys.readouterr().out.splitlines()[0] == (
            f"controller: keeps no saved herdr machines for {SLUG}: AGENTIHOOKS_TERMINAL_CA_KEY is unset"
        )
    finally:
        service.stop()


def test_the_missing_ca_key_line_is_flushed_so_the_container_log_shows_it_at_once(tmp_path, monkeypatch):
    printed = []
    monkeypatch.setattr(_cs(), "print", lambda *args, **kwargs: printed.append((args[0][:41], kwargs)), raising=False)

    service, _, _ = _anton_client(tmp_path, monkeypatch, _store(), **{operator_terminal.CA_KEY_ENV: None})
    try:
        assert printed[0] == ("controller: keeps no saved herdr machines", {"flush": True})
    finally:
        service.stop()


def test_a_controller_without_terminal_exposure_keeps_no_client(tmp_path, monkeypatch):
    service, herdr, environments = _anton_client(tmp_path, monkeypatch, _store(), **{deployed.TERMINAL_ENV: None})
    try:
        assert service.controller.terminals is None
        assert herdr.commands == []
        assert environments == []
    finally:
        service.stop()


def test_a_controller_without_kubernetes_workers_keeps_no_client(tmp_path, monkeypatch):
    changes = {deployed.API_URL_ENV: None, deployed.POLICY_ENV: None}
    service, herdr, environments = _anton_client(tmp_path, monkeypatch, _store(), **changes)
    try:
        assert service.controller.terminals is None
        assert herdr.commands == []
    finally:
        service.stop()


def test_the_kubernetes_runtime_prompts_through_the_controller_terminal(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr.target import DELIVERIES
    from scripts.swarm_v2.kubernetes.prompts import TerminalPrompts
    from scripts.swarm_v2.runtime.base import Capability

    service, _, _ = _anton_client(tmp_path, monkeypatch, _store())
    try:
        kubernetes = service.runtime.router.runtimes[BACKEND]
        terminal = service.controller.terminals.terminal
        assert Capability.COMMAND in kubernetes.capabilities
        assert isinstance(kubernetes.prompts, TerminalPrompts)
        router = kubernetes.prompts.router
        assert router.store is terminal.registrations.store
        assert router.catalog is terminal.registrations.catalog
        assert (router.qualifier, router.run) == (terminal.qualifier, terminal.run)
        assert router.deliveries.path == terminal.home.terminals / DELIVERIES
    finally:
        service.stop()


def test_the_controller_publishes_terminal_views_through_its_client_terminal(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr import visibility
    from scripts.swarm_v2.herdr.capabilities import Qualifier
    from scripts.swarm_v2.herdr.target import DELIVERIES
    from scripts.swarm_v2.kubernetes.terminal_views import TerminalViews

    store = _store()
    service, _, _ = _anton_client(tmp_path, monkeypatch, store)
    try:
        views, terminal = service.views, service.controller.terminals.terminal
        assert isinstance(views, TerminalViews)
        assert (views.store, views.slug) == (store, SLUG)
        viewer, router = views.viewer, views.viewer.router
        assert (viewer.run, viewer.aggregation) == (terminal.run, True)
        assert router.store is terminal.registrations.store
        assert router.catalog is terminal.registrations.catalog
        assert (router.run, router.mutations) == (terminal.run, False)
        assert isinstance(router.qualifier, Qualifier) and router.qualifier is not terminal.qualifier
        assert router.qualifier.probe.__closure__[0].cell_contents is terminal.run
        assert router.deliveries.path == terminal.home.terminals / DELIVERIES
        store.redis.hset(store.key(SLUG, visibility.KEY), "retired", "{}")
        assert service.tick() is True
        service.viewing.join(5)
        assert visibility.read(store, SLUG, 0) == {}
    finally:
        service.stop()


def test_terminal_aggregation_switched_off_builds_a_viewer_without_aggregation(tmp_path, monkeypatch):
    service, _, _ = _anton_client(tmp_path, monkeypatch, _store(), **{deployed.AGGREGATION_ENV: "off"})
    try:
        assert deployed.AGGREGATION_ENV == "AGENTIHOOKS_TERMINAL_AGGREGATION"
        assert service.views.viewer.aggregation is False
    finally:
        service.stop()


def test_a_controller_keeping_no_saved_machines_publishes_no_terminal_views(tmp_path, monkeypatch):
    service, _, _ = _anton_client(tmp_path, monkeypatch, _store(), **{operator_terminal.CA_KEY_ENV: None})
    try:
        assert service.views is None
    finally:
        service.stop()


def test_a_controller_keeping_no_saved_machines_leaves_kubernetes_command_unsupported(tmp_path, monkeypatch):
    from scripts.swarm_v2.runtime.base import Capability

    service, _, _ = _anton_client(tmp_path, monkeypatch, _store(), **{operator_terminal.CA_KEY_ENV: None})
    try:
        kubernetes = service.runtime.router.runtimes[BACKEND]
        assert kubernetes.prompts is None
        assert Capability.COMMAND not in kubernetes.capabilities
    finally:
        service.stop()
