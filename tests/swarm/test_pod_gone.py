import json
import subprocess

import pytest

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.runtime import pod_gone
from scripts.swarm_v2.runtime.base import LOCAL, RuntimeRouter
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.test_swarm_v2_runtime import FakeHerdr

pytestmark = pytest.mark.unit

GONE = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", execution_id="exe-1", runtime_backend="kubernetes")
LISTING = [
    "kubectl",
    "get",
    "pods",
    "--all-namespaces",
    "--selector",
    "swarm.agentihooks.io/execution-id=exe-1",
    "--output",
    "json",
]


class Herdr(FakeHerdr):
    def close_space(self, config: SwarmConfig) -> bool:
        self.calls.append(("close_space", config.slug))
        return True


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    made = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    made.create(SwarmConfig("sw", "/repo"))
    return made


class Runner:
    def __init__(self, code: int = 0, out: bytes = b'{"items": []}', error: Exception | None = None) -> None:
        self.code, self.out, self.error, self.calls = code, out, error, []

    def __call__(self, command: list, **options: object) -> subprocess.CompletedProcess:
        self.calls.append((command, options))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.code, self.out, b"")


def test_no_listed_pod_confirms_the_execution_is_gone() -> None:
    runner = Runner()
    assert pod_gone.absent("exe-1", runner) is True
    assert runner.calls == [(LISTING, {"capture_output": True, "timeout": 10, "check": False})]


@pytest.mark.parametrize(
    "runner",
    [
        Runner(out=json.dumps({"items": [{"metadata": {"name": "p"}}]}).encode()),
        Runner(code=1),
        Runner(out=b"not json"),
        Runner(out=b"[]"),
        Runner(out=b"{}"),
        Runner(error=OSError("no kubectl")),
        Runner(error=subprocess.TimeoutExpired("kubectl", 10)),
    ],
    ids=["pod", "failed", "garbled", "list", "no-items", "missing", "timeout"],
)
def test_anything_but_an_empty_listing_leaves_the_pod_unconfirmed(runner: Runner) -> None:
    assert pod_gone.absent("exe-1", runner) is False


def test_an_agent_with_no_execution_identity_is_never_confirmed_gone() -> None:
    runner = Runner()
    assert pod_gone.absent("", runner) is False
    assert runner.calls == []


def _runtime(absent: bool) -> tuple[RoutedRuntime, list]:
    asked = []

    def probe(execution_id: str) -> bool:
        asked.append(execution_id)
        return absent

    herdr = Herdr()
    router = RuntimeRouter([LocalHerdrRuntime(herdr)], LOCAL)
    return RoutedRuntime(herdr, router, pod_absent=probe), asked


def test_an_unavailable_backend_with_no_pod_retires_the_agent() -> None:
    runtime, asked = _runtime(absent=True)
    assert runtime.retire(GONE) is True
    assert asked == ["exe-1"]


def test_an_unavailable_backend_with_a_pod_keeps_the_agent() -> None:
    runtime, asked = _runtime(absent=False)
    assert runtime.retire(GONE) is False
    assert asked == ["exe-1"]


def test_an_unavailable_local_agent_never_asks_kubernetes() -> None:
    runtime, asked = _runtime(absent=True)
    runtime.router.disabled = frozenset((LOCAL,))
    local = AgentRecord("engineer@a1b2c3-0002", "eng", "t2", pane_id="w1:p1")
    assert runtime.retire(local) is False
    assert asked == []


def test_the_default_probe_is_the_kubectl_listing() -> None:
    herdr = FakeHerdr()
    runtime = RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr)], LOCAL))
    assert runtime.pod_absent is pod_gone.absent


class Ledger:
    def tasks(self, slug: str) -> list:
        return [{"id": "t1", "state": "claimed", "claimed_by": GONE.name}]

    def update_task(self, slug: str, task: str, fields: dict) -> None:
        self.updated = (slug, task, fields)


def test_stop_now_drops_a_kubernetes_agent_whose_pod_is_gone(store) -> None:
    store.put_agent("sw", GONE)
    runtime, _ = _runtime(absent=True)
    ledger = Ledger()
    config, left = cli.stop_now(store, "sw", runtime, ledger)
    assert (config.state, left, store.agents("sw")) == ("stopped", [], [])
    assert ledger.updated == ("sw", "t1", {"state": "open", "claimed_by": ""})
    assert runtime.herdr_runtime.calls == [("close_space", "sw")]
    store.remove("sw")
    assert "sw" not in store.slugs()


def test_stop_now_keeps_a_kubernetes_agent_whose_pod_is_listed(store) -> None:
    store.put_agent("sw", GONE)
    runtime, _ = _runtime(absent=False)
    config, left = cli.stop_now(store, "sw", runtime, Ledger())
    assert (config.state, left, [a.name for a in store.agents("sw")]) == ("stopping", [GONE.name], [GONE.name])
