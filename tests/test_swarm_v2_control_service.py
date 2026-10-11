import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from scripts.hive import auth as hive_auth
from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.runtime import observe

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "control-fixture"
SECRET = b"k" * 32


def _cs():
    from scripts.swarm_v2 import control_service

    return control_service


def _environ(tmp_path, secret=SECRET, key_id="launch-1"):
    path = tmp_path / "launch.key"
    path.write_bytes(secret)
    return {_cs().KEY_ID_ENV: key_id, _cs().KEY_FILE_ENV: str(path)}


def _store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
    return store


def _post(url, token, body, method="POST"):
    request = urllib.request.Request(
        url,
        json.dumps(body).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_launch_key_reads_the_key_file_named_by_the_environment(tmp_path):
    key = _cs().launch_key(_environ(tmp_path))

    assert (key.key_id, key.secret) == ("launch-1", SECRET)


@pytest.mark.parametrize("drop", ["AGENTIHOOKS_LAUNCH_SIGNING_KEY_ID", "AGENTIHOOKS_LAUNCH_SIGNING_KEY_FILE"])
def test_launch_key_refuses_a_missing_setting(tmp_path, drop):
    environ = _environ(tmp_path)
    del environ[drop]

    with pytest.raises(_cs().ControlError, match=drop):
        _cs().launch_key(environ)


def test_launch_key_names_every_missing_setting():
    with pytest.raises(_cs().ControlError) as refused:
        _cs().launch_key({})

    assert str(refused.value) == (
        "the launch signing key needs AGENTIHOOKS_LAUNCH_SIGNING_KEY_ID, AGENTIHOOKS_LAUNCH_SIGNING_KEY_FILE"
    )


def test_launch_key_refuses_a_short_key_without_echoing_it(tmp_path):
    with pytest.raises(_cs().ControlError) as refused:
        _cs().launch_key(_environ(tmp_path, secret=b"short-secret-value"))

    assert str(refused.value) == "signing key must be at least 32 bytes"


def test_launch_key_refuses_a_key_id_that_is_not_an_identifier(tmp_path):
    with pytest.raises(_cs().ControlError) as refused:
        _cs().launch_key(_environ(tmp_path, key_id="not an id"))

    assert str(refused.value) == "signing key ID must be an identifier"


def test_launch_key_refuses_an_unreadable_file(tmp_path):
    environ = {_cs().KEY_ID_ENV: "launch-1", _cs().KEY_FILE_ENV: str(tmp_path / "absent")}

    with pytest.raises(_cs().ControlError, match="unreadable"):
        _cs().launch_key(environ)


def test_the_service_refuses_to_start_without_the_scoped_credential(tmp_path):
    service = _cs().ControlService(_store(), SLUG, _cs().launch_key(_environ(tmp_path)), lambda: False)

    with pytest.raises(Exception, match="scoped controller grant"):
        service.start()


def test_the_service_holds_the_controller_and_renews_it_each_tick(tmp_path):
    service = _cs().ControlService(_store(), SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)

    assert service.start() is True
    epoch = service.controller.held.epoch
    assert service.tick() is True
    assert service.controller.held.epoch == epoch


@pytest.mark.parametrize(("setting", "exporting"), (("stdout", True), ("", False)))
def test_the_service_hands_its_controller_the_configured_transition_exporter(tmp_path, monkeypatch, setting, exporting):
    from scripts.swarm_v2 import telemetry

    monkeypatch.setenv(telemetry.EXPORT_ENV, setting)
    store = _store()

    recorder = (
        _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True).controller.telemetry
    )

    assert recorder.exporter is (telemetry.stdout if exporting else None)
    assert (recorder.store, recorder.slug) == (store, SLUG)


def test_a_second_service_cannot_take_a_held_controller(tmp_path):
    store = _store()
    key = _cs().launch_key(_environ(tmp_path))
    first = _cs().ControlService(store, SLUG, key, lambda: True)
    assert first.start()

    second = _cs().ControlService(store, SLUG, key, lambda: True)

    assert second.start() is False
    assert second.tick() is False


def test_each_tick_runs_the_recovery_pass_only_while_the_controller_is_held(tmp_path):
    store = _store()
    key = _cs().launch_key(_environ(tmp_path))
    service, passes = _cs().ControlService(store, SLUG, key, lambda: True), []
    assert service.tick() is True
    service.recover = lambda: passes.append(service.controller.held.epoch) or {}
    assert service.tick() is True
    assert passes == [service.controller.held.epoch]
    other = _cs().ControlService(store, SLUG, key, lambda: True)
    other.recover = lambda: passes.append("other") or {}
    assert other.tick() is False
    assert passes == [service.controller.held.epoch]


def test_each_tick_publishes_terminal_views_only_while_the_controller_is_held(tmp_path):
    store = _store()
    key = _cs().launch_key(_environ(tmp_path))
    service, passes = _cs().ControlService(store, SLUG, key, lambda: True), []
    assert (service.views, service.viewing) == (None, None)
    assert service.tick() is True
    assert service.viewing is None
    service.views = lambda: passes.append(service.controller.held.epoch) or {}
    assert service.tick() is True
    service.viewing.join(5)
    assert passes == [service.controller.held.epoch]
    assert (service.viewing.name, service.viewing.daemon) == (_cs().VIEWS_THREAD, True)
    other = _cs().ControlService(store, SLUG, key, lambda: True)
    other.views = lambda: passes.append("other") or {}
    assert other.tick() is False
    assert other.viewing is None
    assert passes == [service.controller.held.epoch]


def test_a_slow_views_pass_never_holds_up_the_tick_and_never_overlaps(tmp_path):
    import threading

    service = _cs().ControlService(_store(), SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    release, passes = threading.Event(), []

    def slow():
        passes.append("started")
        release.wait(5)
        return {}

    service.views = slow
    assert service.tick() is True
    first = service.viewing
    assert service.tick() is True
    assert service.viewing is first
    release.set()
    first.join(5)
    assert passes == ["started"]
    assert service.tick() is True
    service.viewing.join(5)
    assert service.viewing is not first
    assert passes == ["started", "started"]
    assert _cs().VIEWS_THREAD == "swarm-v2-terminal-views"


def _admitted(service, store, seat="eng-1@" + SLUG, task="t1"):
    pod = {"pod_namespace": "swarm", "pod_name": f"worker-{task}"}
    record = AgentRecord(
        store.next_name(SLUG, "eng"), "eng", task, seat=seat, runtime_backend=BACKEND, runtime_target=pod
    )
    agent = service.controller.admit(record, "")
    token = service.grants.issue(
        SLUG,
        agent.execution_id,
        project_ids=["github.com/the-cloud-clockwork/agentihooks"],
        brain_id="swarm",
        account="fixture",
    )
    return agent, token


def _beat(service, agent, sequence=1):
    return {
        "schema_version": "2.1",
        "operation_id": f"heartbeat-{sequence}",
        "authority": {
            "execution_id": agent.execution_id,
            "task_id": agent.task,
            "task_generation": 1,
            "controller_epoch": service.controller.held.epoch,
            "owner_identity": agent.name,
        },
        "renewal_sequence": sequence,
        "state": "working",
        "observed_at": "2026-10-10T18:00:00Z",
    }


def _register_and_beat(base, service, agent, token):
    registered = _post(
        f"{base}/v2/executions/register", token, {"execution_id": agent.execution_id, "generation": agent.generation}
    )
    beat = _post(f"{base}/v2/executions/{agent.execution_id}/heartbeat", token, _beat(service, agent), "PUT")
    return registered, beat


def test_a_worker_registers_and_heartbeats_over_http(tmp_path):
    store = _store()
    service = _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    assert service.start()
    agent, token = _admitted(service, store)
    server = service.serve("127.0.0.1", 0)
    try:
        (status, body), (beat_status, ack) = _register_and_beat(
            f"http://127.0.0.1:{server.server_address[1]}", service, agent, token
        )
    finally:
        service.stop()

    assert (status, body["execution_id"]) == (200, agent.execution_id)
    assert (beat_status, ack["renewal_sequence"], ack["execution_id"]) == (200, 1, agent.execution_id)


def test_the_api_thread_is_a_named_daemon(tmp_path):
    service = _cs().ControlService(_store(), SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    service.serve("127.0.0.1", 0)
    try:
        threads = [thread for thread in threading.enumerate() if thread.name == _cs().THREAD]
        assert [thread.daemon for thread in threads] == [True]
    finally:
        service.stop()

    service.thread.join(5)
    assert service.thread.is_alive() is False


def test_stop_releases_the_lease_so_a_restart_takes_it_at_once(tmp_path):
    store = _store()
    key = _cs().launch_key(_environ(tmp_path))
    first = _cs().ControlService(store, SLUG, key, lambda: True)
    assert first.start()

    first.stop()

    assert lease.current(store, SLUG) is None
    assert _cs().ControlService(store, SLUG, key, lambda: True).start() is True


def test_the_service_shares_the_lease_with_the_hive_tick(tmp_path):
    store = _store()
    service = _cs().ControlService(
        store, SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True, owner="hive-fixture"
    )
    assert service.start()

    tick_lease = lease.acquire(store, SLUG, "hive-fixture")

    assert (tick_lease.owner, tick_lease.epoch) == ("hive-fixture", service.controller.held.epoch)
    assert service.tick() is True


def test_a_tick_takes_the_controller_again_after_its_lease_lapsed(tmp_path):
    store = _store()
    service = _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    assert service.start()
    epoch = service.controller.held.epoch
    store.redis.delete(store.key(SLUG, "control-owner"))

    assert service.tick() is True
    assert (service.controller.held.epoch, service.controller.ready) == (epoch + 1, True)


def test_a_tick_sends_each_remote_heartbeat_through_the_controller(tmp_path):
    store = _store()
    service = _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    assert service.start()
    beating, token = _admitted(service, store)
    silent, _ = _admitted(service, store, seat=f"eng-2@{SLUG}", task="t2")
    local = service.controller.admit(AgentRecord(store.next_name(SLUG, "eng"), "eng", "t3", seat=f"eng-3@{SLUG}"), "")
    service.executions.register(token, {"execution_id": beating.execution_id, "generation": beating.generation})
    service.executions.heartbeat(beating.execution_id, token, _beat(service, beating))
    accepted = json.loads(store.redis.hget(store.key(SLUG, "heartbeats"), beating.execution_id))["accepted_at_ms"]

    assert service.tick(now=accepted / 1000 + 1) is True

    seen = observe.stored(store, SLUG, beating.execution_id)
    assert seen.sources == {
        "heartbeat": {"reading": "ok", "observed_at": accepted / 1000, "value": "working"},
    }
    assert seen.state.value == "working"
    assert observe.stored(store, SLUG, silent.execution_id).sources == {}
    assert observe.stored(store, SLUG, local.execution_id) is None


def test_hosting_is_off_without_an_api_port(tmp_path):
    assert _cs().host(_environ(tmp_path), _store(), "hive-fixture") is None


def test_hosting_needs_the_swarm_it_serves(tmp_path):
    environ = {**_environ(tmp_path), _cs().PORT_ENV: "8780"}

    with pytest.raises(_cs().ControlError) as refused:
        _cs().host(environ, _store(), "hive-fixture")

    assert str(refused.value) == "the control API needs AGENTIHOOKS_CONTROL_SWARM"


def test_hosting_refuses_a_controller_credential_the_hive_did_not_issue(tmp_path):
    store = _store()
    hive_auth.issue_controller(store.redis)
    environ = {
        **_environ(tmp_path),
        _cs().PORT_ENV: "8780",
        _cs().SWARM_ENV: SLUG,
        _cs().CREDENTIAL_ENV: "forged",
    }

    with pytest.raises(SwarmError, match="scoped controller grant"):
        _cs().host(environ, store, "hive-fixture")

    assert lease.current(store, SLUG) is None


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_controller_loop_hosts_registration_and_heartbeats(tmp_path, monkeypatch):
    from scripts.swarm import controller as loop

    store = _store()
    port = _free_port()
    environ = {
        **_environ(tmp_path),
        _cs().PORT_ENV: str(port),
        _cs().SWARM_ENV: SLUG,
        _cs().CREDENTIAL_ENV: hive_auth.issue_controller(store.redis),
        "SWARM_HIVE_ID": "hive-fixture",
    }
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    hosted, seen = [], []
    real_host = _cs().host
    monkeypatch.setattr(_cs(), "host", lambda *args: hosted.append(real_host(*args)) or hosted[0])
    monkeypatch.setattr(loop, "connect", lambda: store)
    monkeypatch.setattr("scripts.operator_env.fill", lambda env: None)

    def tick(given):
        service = hosted[0]
        agent, token = _admitted(service, given)
        seen.append((agent, _register_and_beat(f"http://127.0.0.1:{port}", service, agent, token)))
        return {SLUG: ["ticked"]}

    monkeypatch.setattr(loop, "run_once", tick)

    assert loop.main(["run", "--once"]) == 0

    agent, ((status, _), (beat_status, ack)) = seen[0]
    assert (status, beat_status, ack["controller_epoch"]) == (200, 200, 1)
    assert hosted[0].controller.owner == "hive-fixture"
    assert observe.stored(store, SLUG, agent.execution_id).sources["heartbeat"]["value"] == "working"
    assert lease.current(store, SLUG) is None


@pytest.mark.parametrize("port", ["eighty", "0", "65536"])
def test_hosting_refuses_a_port_outside_the_tcp_range(tmp_path, port):
    environ = {**_environ(tmp_path), _cs().PORT_ENV: port, _cs().SWARM_ENV: SLUG}

    with pytest.raises(_cs().ControlError) as refused:
        _cs().host(environ, _store(), "hive-fixture")

    assert str(refused.value) == "AGENTIHOOKS_CONTROL_API_PORT must be a port number"


def _hosted_environ(tmp_path, store, port):
    return {
        **_environ(tmp_path),
        _cs().PORT_ENV: str(port),
        _cs().SWARM_ENV: SLUG,
        _cs().CREDENTIAL_ENV: hive_auth.issue_controller(store.redis),
    }


def test_the_api_opens_only_once_the_controller_holds_the_lease(tmp_path):
    store = _store()
    rival = lease.acquire(store, SLUG, "hive-rival")

    service = _cs().host(_hosted_environ(tmp_path, store, _free_port()), store, "hive-fixture")
    try:
        assert (service.server, service.tick()) == (None, False)
        lease.release(store, SLUG, rival)

        assert service.tick() is True
        assert service.server is not None
    finally:
        service.stop()


def test_the_hosted_api_serves_shallow_liveness_and_guards_diagnostics(tmp_path, monkeypatch):
    from scripts.swarm_v2 import worker_health

    probed = []
    monkeypatch.setattr(_cs(), "page_credential", lambda slug: "operator-fixture")
    monkeypatch.setattr(
        worker_health, "brain_state", lambda environ, timeout: probed.append(timeout) or "brain_http_401"
    )
    store = _store()
    port = _free_port()
    service = _cs().host(_hosted_environ(tmp_path, store, port), store, "hive-fixture")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/health/live", timeout=5) as response:
            live = json.loads(response.read())
        status, refused = _post(f"http://127.0.0.1:{port}/v2/health/qualify", "stranger", {})
        found, report = _post(
            f"http://127.0.0.1:{port}/v2/health/qualify",
            "operator-fixture",
            {"purpose": "backup", "report": live},
        )
    finally:
        service.stop()
    assert (live["probe"], live["status"]) == ("shallow", "live")
    assert (status, refused["error_class"]) == (401, "unauthenticated")
    assert (found, report["reason"]) == (409, "shallow_report")
    assert service.health.controller is service.controller
    assert (service.health.store, service.health_api.grants) == (store, service.grants)
    assert service.health.diagnose()["dependencies"]["brain"]["state"] == "auth_failed"
    assert probed == [worker_health.BRAIN_TIMEOUT]


def test_the_hosted_api_honours_the_deep_diagnostics_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_DEEP", "off")
    store = _store()
    port = _free_port()
    service = _cs().host(_hosted_environ(tmp_path, store, port), store, "hive-fixture")
    try:
        status, refused = _post(f"http://127.0.0.1:{port}/v2/health/qualify", "anyone", {})
    finally:
        service.stop()
    assert (status, refused["message"]) == (503, "deep diagnostics are switched off")


def test_the_page_credential_comes_from_the_ledger_repository(monkeypatch):
    from scripts.swarm_ledger.repository import repository

    monkeypatch.setattr(repository, "token", lambda slug: f"token-for-{slug}")
    assert _cs().page_credential(SLUG) == f"token-for-{SLUG}"


def test_a_port_it_cannot_bind_releases_the_lease(tmp_path):
    store = _store()
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        environ = _hosted_environ(tmp_path, store, taken.getsockname()[1])

        with pytest.raises(OSError):
            _cs().host(environ, store, "hive-fixture")

    assert lease.current(store, SLUG) is None


def test_the_controller_loop_runs_as_before_without_an_api_port(tmp_path, monkeypatch):
    from scripts.swarm import controller as loop

    store = _store()
    monkeypatch.delenv(_cs().PORT_ENV, raising=False)
    monkeypatch.setattr(loop, "connect", lambda: store)
    monkeypatch.setattr("scripts.operator_env.fill", lambda env: None)
    ticked = []
    monkeypatch.setattr(loop, "run_once", lambda given: ticked.append(given) or {})

    assert loop.main(["run", "--once"]) == 0

    assert ticked == [store]
    assert lease.current(store, SLUG) is None
    assert [thread for thread in threading.enumerate() if thread.name == _cs().THREAD] == []


@pytest.mark.parametrize("port", ["1", "65535"])
def test_hosting_takes_each_end_of_the_tcp_range(tmp_path, port):
    environ = {**_environ(tmp_path), _cs().PORT_ENV: port, _cs().SWARM_ENV: SLUG}

    with pytest.raises(SwarmError, match="scoped controller grant"):
        _cs().host(environ, _store(), "hive-fixture")


def test_a_missing_controller_credential_never_matches_a_stored_grant(tmp_path):
    store = _store()
    store.redis.set(f"{hive_auth.PREFIX}:controller", hive_auth._digest("XXXX"))
    environ = {**_environ(tmp_path), _cs().PORT_ENV: str(_free_port()), _cs().SWARM_ENV: SLUG}

    with pytest.raises(SwarmError, match="scoped controller grant"):
        _cs().host(environ, store, "hive-fixture")


def test_the_api_thread_runs_the_server_loop(tmp_path, monkeypatch):
    started = []

    class Thread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self.kwargs)

    monkeypatch.setattr(_cs().threading, "Thread", Thread)
    service = _cs().ControlService(_store(), SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    server = service.serve("127.0.0.1", 0)
    try:
        assert started == [{"target": server.serve_forever, "name": _cs().THREAD, "daemon": True}]
    finally:
        server.server_close()


def test_a_tick_observes_at_the_store_clock_in_seconds(tmp_path, monkeypatch):
    service = _cs().ControlService(_store(), SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    assert service.start()
    observed = []
    monkeypatch.setattr(_cs().lease, "now_ms", lambda store: 5_000_000)
    monkeypatch.setattr(service, "observe", observed.append)

    assert service.tick() is True

    assert observed == [5000.0]


def test_observe_returns_only_the_findings_the_controller_raised(tmp_path, monkeypatch):
    store = _store()
    service = _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    assert service.start()
    _admitted(service, store)
    _admitted(service, store, seat=f"eng-2@{SLUG}", task="t2")
    finding = object()
    answers = iter([finding, None])
    monkeypatch.setattr(service.controller, "observe", lambda *args: next(answers))

    assert service.observe(1.0) == [finding]


def test_the_controller_loop_reports_a_hosting_refusal(monkeypatch, capsys):
    from scripts.swarm import controller as loop

    def refuse(*args):
        raise _cs().ControlError("the control API needs AGENTIHOOKS_CONTROL_SWARM")

    monkeypatch.setattr(_cs(), "host", refuse)
    monkeypatch.setattr(loop, "connect", _store)
    monkeypatch.setattr("scripts.operator_env.fill", lambda env: None)

    assert loop.main(["run", "--once"]) == 1

    assert capsys.readouterr().err == "controller: the control API needs AGENTIHOOKS_CONTROL_SWARM\n"


LEDGER_TASK = {
    "id": "t1",
    "title": "Read through the controller",
    "description": "The worker reads its task from the controller ledger",
    "phase": "p1",
    "lane": "eng",
    "state": "claimed",
    "done": False,
}


def _seed_ledger(folder):
    from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
    from tests.swarm_ledger.test_tasks import core, new_ledger

    folder.mkdir(parents=True)
    content = {
        "title": "Controller ledger fixture",
        "overview": "Tasks the controller serves to its workers",
        "sources": [],
        "phases": [{"title": "Ledger", "description": "Tasks read through the controller"}],
        "tasks": [],
    }
    document = new_ledger.build_doc(content)
    document["tasks"] = [dict(LEDGER_TASK)]
    document, meta, _ = core.load_state(folder / "seed.json", document)
    SQLiteLedgerRepository(folder / DATABASE, core).create_document(SLUG, document, meta)


def _ledger_hosted(tmp_path, store, port):
    folder = tmp_path / "ledger"
    _seed_ledger(folder)
    return {**_hosted_environ(tmp_path, store, port), "LEDGER_DIR": str(folder)}, folder


def _get(url, token):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _registered_read(service, store, port):
    agent, token = _admitted(service, store)
    base = f"http://127.0.0.1:{port}"
    (registered, _), _ = _register_and_beat(base, service, agent, token)
    return registered, token, _get(f"{base}/v2/tasks/t1", token)


def test_the_controller_start_serves_ledger_tasks_beside_the_worker_api(tmp_path):
    from scripts.swarm_v2.api.tasks import spec_revision

    store = _store()
    port = _free_port()
    environ, _ = _ledger_hosted(tmp_path, store, port)
    service = _cs().host(environ, store, "hive-fixture")
    try:
        registered, token, (status, body) = _registered_read(service, store, port)
        controls = _get(f"http://127.0.0.1:{port}/v2/swarm", token)
    finally:
        service.stop()

    assert (registered, status, body["task_id"]) == (200, 200, "t1")
    assert {name: body["task"][name] for name in ("id", "title", "description", "phase", "lane", "state")} == {
        name: LEDGER_TASK[name] for name in ("id", "title", "description", "phase", "lane", "state")
    }
    assert body["revision"] == spec_revision(body["task"])
    assert (controls[0], controls[1]["error_class"], controls[1]["message"]) == (
        403,
        "forbidden_scope",
        "swarm controls need the operator",
    )
    assert service.tasks_api.writes_enabled is True


def test_the_ledger_writer_lease_waits_until_the_controller_holds_its_lease(tmp_path):
    from scripts.swarm_v2 import ledger_writer

    store = _store()
    environ, folder = _ledger_hosted(tmp_path, store, _free_port())
    rival = lease.acquire(store, SLUG, "hive-rival")
    service = _cs().host(environ, store, "hive-fixture")
    try:
        probe = ledger_writer.WriterLease(folder).acquire(ledger_writer.owner())
        probe.release()
        assert (service.tasks_api, service.writer) == (None, None)
        lease.release(store, SLUG, rival)

        assert service.tick() is True
        assert (service.writer.held, service.tasks_api.writes_enabled) == (True, True)
    finally:
        service.stop()


def test_the_controller_holds_the_ledger_writer_lease_until_it_stops(tmp_path):
    from scripts.swarm_v2 import ledger_writer

    store = _store()
    environ, folder = _ledger_hosted(tmp_path, store, _free_port())
    service = _cs().host(environ, store, "hive-fixture")
    try:
        with pytest.raises(ledger_writer.WriterConflict):
            ledger_writer.WriterLease(folder).acquire(ledger_writer.owner())
    finally:
        service.stop()

    after = ledger_writer.WriterLease(folder).acquire(ledger_writer.owner())
    assert after.held is True
    after.release()


def test_a_ledger_folder_another_writer_holds_serves_tasks_read_only(tmp_path, monkeypatch):
    from scripts.swarm_v2 import ledger_writer

    printed = []
    monkeypatch.setattr(_cs(), "print", lambda *args, **kwargs: printed.append((args[0], kwargs)), raising=False)
    store = _store()
    port = _free_port()
    environ, folder = _ledger_hosted(tmp_path, store, port)
    rival = ledger_writer.WriterLease(folder).acquire(ledger_writer.owner())
    try:
        service = _cs().host(environ, store, "hive-fixture")
        try:
            _, token, (status, read) = _registered_read(service, store, port)
            write = {
                "operation_id": "op-1",
                "task_generation": 1,
                "expected_revision": read["revision"],
                "fields": {"state": "pr"},
            }
            refused_status, refused = _post(f"http://127.0.0.1:{port}/v2/tasks/t1", token, write, "PATCH")
        finally:
            service.stop()
        with pytest.raises(ledger_writer.WriterConflict):
            ledger_writer.WriterLease(folder).acquire(ledger_writer.owner())
    finally:
        rival.release()

    assert status == 200
    assert (refused_status, refused["error_class"], refused["read_only"]) == (409, "revision_conflict", True)
    assert service.tasks_api.writes_enabled is False
    assert printed[0] == (
        f"controller: serves the tasks API read only for {SLUG}: ledger folder {folder} already has an active "
        f"writer, pid {ledger_writer.owner()['pid']} on {ledger_writer.owner()['host']}; a second writer is refused",
        {"flush": True},
    )


def test_a_service_without_a_ledger_folder_serves_only_the_worker_api(tmp_path):
    store = _store()
    port = _free_port()
    service = _cs().ControlService(store, SLUG, _cs().launch_key(_environ(tmp_path)), lambda: True)
    service.listen = ("127.0.0.1", port)
    try:
        assert service.start() is True
        agent, token = _admitted(service, store)
        (registered, _), _ = _register_and_beat(f"http://127.0.0.1:{port}", service, agent, token)
        status, refused = _get(f"http://127.0.0.1:{port}/v2/tasks/t1", token)
    finally:
        service.stop()

    assert (service.ledger_folder, service.tasks_api, service.writer) == (None, None, None)
    assert (registered, status, refused["message"]) == (200, 404, "no such execution endpoint")
