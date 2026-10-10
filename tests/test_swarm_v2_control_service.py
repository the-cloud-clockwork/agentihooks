import json
import urllib.error
import urllib.request

import fakeredis
import pytest
from scripts.swarm_v2.control_service import ControlService, launch_key

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import control_service

SLUG = "control-fixture"
SECRET = b"k" * 32


def _environ(tmp_path, secret=SECRET, key_id="launch-1"):
    path = tmp_path / "launch.key"
    path.write_bytes(secret)
    return {control_service.KEY_ID_ENV: key_id, control_service.KEY_FILE_ENV: str(path)}


def _store():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
    return store


def _post(url, token, body):
    request = urllib.request.Request(
        url, json.dumps(body).encode(), {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_launch_key_reads_the_key_file_named_by_the_environment(tmp_path):
    key = launch_key(_environ(tmp_path))

    assert (key.key_id, key.secret) == ("launch-1", SECRET)


@pytest.mark.parametrize("drop", [control_service.KEY_ID_ENV, control_service.KEY_FILE_ENV])
def test_launch_key_refuses_a_missing_setting(tmp_path, drop):
    environ = _environ(tmp_path)
    del environ[drop]

    with pytest.raises(control_service.ControlError, match=drop):
        launch_key(environ)


def test_launch_key_refuses_a_short_key_without_echoing_it(tmp_path):
    with pytest.raises(control_service.ControlError) as refused:
        launch_key(_environ(tmp_path, secret=b"short-secret-value"))

    assert "short-secret-value" not in str(refused.value)


def test_launch_key_refuses_an_unreadable_file(tmp_path):
    environ = {control_service.KEY_ID_ENV: "launch-1", control_service.KEY_FILE_ENV: str(tmp_path / "absent")}

    with pytest.raises(control_service.ControlError, match="unreadable"):
        launch_key(environ)


def test_the_service_refuses_to_start_without_the_scoped_credential(tmp_path):
    service = ControlService(_store(), SLUG, launch_key(_environ(tmp_path)), lambda: False)

    with pytest.raises(Exception, match="scoped controller grant"):
        service.start()


def test_the_service_holds_the_controller_and_renews_it_each_tick(tmp_path):
    service = ControlService(_store(), SLUG, launch_key(_environ(tmp_path)), lambda: True)

    assert service.start() is True
    epoch = service.controller.held.epoch
    assert service.tick() is True
    assert service.controller.held.epoch == epoch


def test_a_second_service_cannot_take_a_held_controller(tmp_path):
    store = _store()
    key = launch_key(_environ(tmp_path))
    first = ControlService(store, SLUG, key, lambda: True)
    assert first.start()

    second = ControlService(store, SLUG, key, lambda: True)

    assert second.start() is False
    assert second.tick() is False


def test_a_worker_registers_over_http_and_its_heartbeat_reaches_the_api(tmp_path):
    store = _store()
    service = ControlService(store, SLUG, launch_key(_environ(tmp_path)), lambda: True)
    assert service.start()
    agent = service.controller.admit(AgentRecord(store.next_name(SLUG, "eng"), "eng", "t1", seat="eng-1"), "")
    token = service.grants.issue(SLUG, agent.execution_id, project_ids=["p"], brain_id="swarm", account="fixture")
    server = service.serve("127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _post(
            f"{base}/v2/executions/register",
            token,
            {"execution_id": agent.execution_id, "generation": agent.generation},
        )
        refused, detail = _post(f"{base}/v2/executions/{agent.execution_id}/heartbeat", token, {})
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert body["execution_id"] == agent.execution_id
    assert refused != 404
    assert "error" in json.dumps(detail)
