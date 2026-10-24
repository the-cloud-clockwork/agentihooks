import json
import multiprocessing

import pytest

from scripts.swarm import controller, lease
from scripts.swarm.store import RedisStore, SwarmError

pytestmark = pytest.mark.unit


@pytest.fixture
def locked_store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(store, "sw", "host")
    return store, held


def hold_tick(store, held, sender):
    sender.send(controller.take_tick_lock(store, "sw", held, 600_000))
    sender.recv()


def test_a_killed_tick_holder_is_replaced_without_waiting_for_expiry(locked_store):
    store, held = locked_store
    receiver, sender = multiprocessing.Pipe()
    child = multiprocessing.get_context("fork").Process(target=hold_tick, args=(store, held, sender))
    child.start()
    try:
        assert receiver.poll(10)
        original = receiver.recv()
        store.redis.set(store.key("sw", "tick-lock"), original, px=600_000)
        assert controller.take_tick_lock(store, "sw", held, 600_000) is None
        child.kill()
        child.join(10)
        assert child.exitcode == -9
        assert store.redis.pttl(store.key("sw", "tick-lock")) > 590_000
        replacement = controller.take_tick_lock(store, "sw", held, 600_000)
        assert replacement is not None and replacement != original
        controller.release_tick_lock(store, "sw", original)
        assert store.redis.get(store.key("sw", "tick-lock")) == replacement
        with pytest.raises(SwarmError, match="tick lock is stale"):
            controller.keep_tick(store, "sw", held, original, 600_000)
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)
        receiver.close()
        sender.close()


def test_a_live_tick_holder_keeps_its_lock(locked_store):
    store, held = locked_store
    original = controller.take_tick_lock(store, "sw", held, 600_000)
    assert original is not None
    assert controller.take_tick_lock(store, "sw", held, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original


def test_a_newer_epoch_tick_leaves_a_live_holders_lock_alone(locked_store):
    store, held = locked_store
    original = controller.take_tick_lock(store, "sw", held, 600_000)
    assert lease.release(store, "sw", held)
    current = lease.acquire(store, "sw", "successor")
    assert current.epoch == held.epoch + 1
    assert controller.take_tick_lock(store, "sw", current, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original
    with pytest.raises(SwarmError, match="stale"):
        controller.take_tick_lock(store, "sw", held, 600_000)
    store.redis.delete(store.key("sw", "tick-lock"))
    replacement = controller.take_tick_lock(store, "sw", current, 600_000)
    assert replacement is not None
    controller.release_tick_lock(store, "sw", original)
    assert store.redis.get(store.key("sw", "tick-lock")) == replacement


@pytest.mark.parametrize("legacy", [True, False])
def test_a_lock_without_a_verifiable_holder_is_not_stolen(locked_store, legacy):
    store, held = locked_store
    original = "legacy" if legacy else json.dumps({"epoch": held.epoch, "token": "legacy"})
    store.redis.set(store.key("sw", "tick-lock"), original, px=600_000)
    assert controller.take_tick_lock(store, "sw", held, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original


STAT = "7 (tick (x)) " + " ".join(["S", *map(str, range(4, 52))])
SHORT = "7 (x) " + " ".join(["S", *map(str, range(4, 22))])
EXACT = "7 (x) " + " ".join(["S", *map(str, range(4, 23))])


def test_the_stat_fields_follow_the_process_name(monkeypatch):
    monkeypatch.setattr(controller, "_read", lambda path: STAT if path == "/proc/7/stat" else "")
    assert controller._stat(7)[:2] == ["S", "4"]
    assert controller._stat(7)[19] == "22"
    assert controller._stat(8) == []


def test_the_host_names_the_boot_and_the_pid_namespace(monkeypatch):
    monkeypatch.setattr(controller, "_read", lambda path: "boot" if path == "/proc/sys/kernel/random/boot_id" else "")
    monkeypatch.setattr(controller.os, "readlink", lambda path: f"ns of {path}")
    assert controller._host() == "boot:ns of /proc/self/ns/pid"
    monkeypatch.setattr(controller, "_read", lambda path: "")
    assert controller._host() == ""


def test_an_unreadable_pid_namespace_names_no_host(monkeypatch):
    def refused(path):
        raise PermissionError(path)

    monkeypatch.setattr(controller, "_read", lambda path: "boot")
    monkeypatch.setattr(controller.os, "readlink", refused)
    assert controller._host() == ""


@pytest.mark.parametrize(
    ("host", "stat", "holder"), [("h", STAT, True), ("h", EXACT, True), ("", STAT, False), ("h", SHORT, False)]
)
def test_a_holder_is_recorded_only_with_its_host_and_start(monkeypatch, host, stat, holder):
    pid = controller.os.getpid()
    monkeypatch.setattr(controller, "_host", lambda: host)
    monkeypatch.setattr(controller, "_read", lambda path: stat if path == f"/proc/{pid}/stat" else "")
    assert controller._holder() == ({"host": host, "pid": pid, "start": "22"} if holder else {})


@pytest.mark.parametrize(
    ("signal", "stat", "start", "gone"),
    [
        (ProcessLookupError, STAT, "22", True),
        (PermissionError, "", "22", False),
        (None, STAT, "22", False),
        (None, STAT, "21", True),
        (None, STAT.replace(") S ", ") Z "), "22", True),
        (None, SHORT, "22", False),
        (None, EXACT, "21", True),
        (None, "", "22", False),
    ],
)
def test_a_holder_is_gone_only_when_its_process_is_missing_reused_or_a_zombie(monkeypatch, signal, stat, start, gone):
    signals = []

    def kill(pid, number):
        signals.append((pid, number))
        if signal:
            raise signal(pid)

    monkeypatch.setattr(controller.os, "kill", kill)
    monkeypatch.setattr(controller, "_read", lambda path: stat if path == "/proc/7/stat" else "")
    assert controller._gone(7, start) is gone
    assert signals == [(7, 0)]


@pytest.mark.parametrize(
    ("host", "lock", "dead"),
    [
        ("h", {"host": "h", "pid": 7, "start": "old"}, True),
        ("h", {"host": "h", "pid": 8, "start": "old"}, False),
        ("h", {"host": "other", "pid": 7, "start": "old"}, False),
        ("h", {"host": "h", "pid": 7}, False),
        ("h", {"host": "h", "start": "old"}, False),
        ("", {"host": "", "pid": 7, "start": "old"}, False),
    ],
)
def test_only_a_local_holder_whose_process_is_gone_is_dead(monkeypatch, host, lock, dead):
    asked = []
    monkeypatch.setattr(controller, "_host", lambda: host)
    monkeypatch.setattr(controller, "_gone", lambda pid, start: asked.append((pid, start)) or pid == 7)
    assert controller._dead(json.dumps({"epoch": 1, "token": "t", **lock})) is dead
    assert controller._dead("legacy") is False
    assert asked == (
        [(lock["pid"], "old")] if host and lock["host"] == host and {"pid", "start"} <= lock.keys() else []
    )


@pytest.mark.parametrize("machine", ["m", "XXXX"])
def test_a_container_names_no_machine_so_no_holder_is_dead_by_boot(monkeypatch, tmp_path, machine):
    mark = tmp_path / "dockerenv"
    monkeypatch.setattr(controller, "CONTAINER_MARKS", (str(mark),))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(controller, "_read", lambda path: machine if path == "/etc/machine-id" else "")
    held = {"host": "oldboot:ns", "machine": machine}
    assert controller._contained() is False
    assert controller._ended_boot(held, "boot:ns") is True
    mark.write_text("")
    assert controller._contained() is True
    assert controller._ended_boot(held, "boot:ns") is False
    mark.unlink()
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert controller._contained() is True
    assert controller._ended_boot(held, "boot:ns") is False


def test_read_strips_a_file_and_reads_a_missing_one_as_empty(tmp_path):
    path = tmp_path / "boot_id"
    path.write_text("abc\n")
    assert controller._read(str(path)) == "abc"
    assert controller._read(str(tmp_path / "missing")) == ""


OWN_POD = {
    "AGENTIHOOKS_POD_NAMESPACE": "swarm",
    "AGENTIHOOKS_POD_NAME": "controller-new",
    "AGENTIHOOKS_POD_UID": "uid-new",
}
OLD_POD = {"namespace": "swarm", "name": "controller-old", "uid": "uid-old"}


@pytest.fixture
def replaced_pod(monkeypatch, locked_store):
    from scripts.swarm_v2.kubernetes import client

    store, held = locked_store
    reads = []
    answer = {"pod": None}

    def read_pod(self, name):
        reads.append((self.namespace, name))
        if isinstance(answer["pod"], Exception):
            raise answer["pod"]
        return answer["pod"]

    for name, value in OWN_POD.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "_host", lambda: "boot:pid:[new]")
    monkeypatch.setattr(client.KubeHttp, "in_cluster", classmethod(lambda cls, environ: object()))
    monkeypatch.setattr(client.PodClient, "read_pod", read_pod)

    def lock(pod):
        raw = json.dumps(
            {"epoch": held.epoch, "token": "old", "host": "boot:pid:[old]", "pid": 1, "start": "9", "pod": pod}
        )
        store.redis.set(store.key("sw", "tick-lock"), raw, px=600_000)
        return raw

    return store, held, answer, reads, lock


@pytest.mark.parametrize(
    "pod",
    [
        None,
        {"metadata": {"uid": "uid-other"}, "status": {"phase": "Running"}},
        {"metadata": {"uid": "uid-old"}, "status": {"phase": "Failed"}},
        {"metadata": {"uid": "uid-old"}, "status": {"phase": "Succeeded"}},
    ],
)
def test_a_lock_held_by_a_replaced_pod_is_recovered_at_once(replaced_pod, pod):
    store, held, answer, reads, lock = replaced_pod
    answer["pod"] = pod
    lock(OLD_POD)
    token = controller.take_tick_lock(store, "sw", held, 600_000)
    assert token is not None
    assert store.redis.get(store.key("sw", "tick-lock")) == token
    assert json.loads(token)["pod"] == {"namespace": "swarm", "name": "controller-new", "uid": "uid-new"}
    assert reads == [("swarm", "controller-old")]


def this_pod(container_id, running=True):
    status = {"name": "controller", "containerID": container_id, "state": {"running": {}} if running else {}}
    return {"metadata": {"uid": "uid-new"}, "status": {"phase": "Running", "containerStatuses": [status]}}


@pytest.mark.parametrize(
    ("pod", "recovered"),
    [(this_pod("c-new"), True), (this_pod("c-old", running=False), True), (this_pod("c-old"), False)],
)
def test_a_lock_from_another_container_of_this_pod_is_recovered_only_once_that_container_stopped(
    replaced_pod, pod, recovered
):
    store, held, answer, reads, lock = replaced_pod
    answer["pod"] = pod
    original = lock({"namespace": "swarm", "name": "controller-new", "uid": "uid-new", "container": "c-old"})
    token = controller.take_tick_lock(store, "sw", held, 600_000)
    assert (token is not None) is recovered
    assert store.redis.get(store.key("sw", "tick-lock")) == (token if recovered else original)
    assert reads == [("swarm", "controller-new")]


@pytest.mark.parametrize(
    "pod",
    [
        {"metadata": {"uid": "uid-old"}, "status": {"phase": "Running"}},
        {"metadata": {"uid": "uid-old"}, "status": {"phase": "Pending"}},
        {"metadata": {"uid": "uid-old"}},
        ConnectionError("API server answered 503"),
        PermissionError("forbidden"),
    ],
)
def test_a_live_holder_in_another_namespace_keeps_its_lock(replaced_pod, pod):
    from scripts.swarm_v2.kubernetes.client import ApiRefused

    store, held, answer, reads, lock = replaced_pod
    answer["pod"] = ApiRefused(403, "Forbidden") if isinstance(pod, PermissionError) else pod
    original = lock({"namespace": "other", "name": "controller-old", "uid": "uid-old"})
    assert controller.take_tick_lock(store, "sw", held, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original
    assert reads == [("other", "controller-old")]


@pytest.mark.parametrize(
    "pod",
    [{}, {"namespace": "swarm", "name": "controller-old"}, {"namespace": "swarm", "name": "", "uid": "u"}, "pod", None],
)
def test_a_holder_without_a_full_pod_identity_is_not_judged_by_its_pod(replaced_pod, pod):
    store, held, answer, reads, lock = replaced_pod
    original = lock(pod)
    assert controller.take_tick_lock(store, "sw", held, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original
    assert reads == []


def test_a_recovering_process_outside_a_pod_never_judges_a_pod_holder(replaced_pod, monkeypatch):
    store, held, answer, reads, lock = replaced_pod
    monkeypatch.delenv("AGENTIHOOKS_POD_UID")
    original = lock(OLD_POD)
    assert controller.take_tick_lock(store, "sw", held, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original
    assert reads == []


@pytest.mark.parametrize(
    ("machine", "lock", "dead"),
    [
        ("m", {"host": "oldboot:ns", "pid": 7, "start": "s", "machine": "m"}, True),
        ("m", {"host": "boot:ns", "pid": 8, "start": "s", "machine": "m"}, False),
        ("m", {"host": "oldboot:ns", "pid": 8, "start": "s", "machine": "other"}, False),
        ("m", {"host": "oldboot:ns", "pid": 8, "start": "s"}, False),
        ("m", {"pid": 8, "start": "s", "machine": "m"}, False),
        ("m", {"host": "boot", "pid": 8, "start": "s", "machine": "m"}, False),
        ("", {"host": "oldboot:ns", "pid": 8, "start": "s", "machine": ""}, False),
    ],
)
def test_a_holder_from_an_ended_boot_of_this_machine_is_dead(monkeypatch, machine, lock, dead):
    monkeypatch.setattr(controller, "_contained", lambda: False)
    monkeypatch.setattr(controller, "_host", lambda: "boot:ns")
    monkeypatch.setattr(controller, "_read", lambda path: machine if path == "/etc/machine-id" else "")
    monkeypatch.setattr(controller, "_gone", lambda pid, start: pid == 7)
    assert controller._dead(json.dumps({"epoch": 1, "token": "t", **lock})) is dead


def test_a_holder_records_its_machine_when_one_is_known(monkeypatch):
    pid = controller.os.getpid()
    monkeypatch.setattr(controller, "_host", lambda: "h")
    monkeypatch.setattr(
        controller, "_read", lambda path: {f"/proc/{pid}/stat": STAT, "/etc/machine-id": "m"}.get(path, "")
    )
    assert controller._holder() == {"host": "h", "pid": pid, "start": "22", "machine": "m"}
