import json

from tests.hive.test_controller_launch import setup  # noqa: F401


def _register(path) -> None:
    from scripts import install

    state = json.loads(install.STATE_JSON.read_text()) if install.STATE_JSON.exists() else {}
    install.STATE_JSON.write_text(json.dumps({**state, "user_bundle": {"path": str(path)}}))


def _request(setup):  # noqa: F811
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_v2.runtime.base import SpawnRequest

    store, slug = setup.store, setup.config.slug
    name = store.next_name(slug, "eng")
    record = AgentRecord(name, "eng", "task", seat=f"eng-1@{slug}", state="starting")
    store.put_agent(slug, record)
    store.seats.occupy(record.seat, name, 0)
    task = {"id": "task", "seat": record.seat, "controller_epoch": setup.held.epoch}
    return SpawnRequest(setup.config, "eng", name, task)


def _unreserved(setup, monkeypatch):  # noqa: F811
    from unittest.mock import Mock

    spy = Mock(side_effect=AssertionError("a refused seat must not reserve room"))
    monkeypatch.setattr(setup.launcher, "reserve_room", spy)
    return spy


def _published(setup) -> int:  # noqa: F811
    from scripts.hive.orders import Orders

    return setup.store.redis.xlen(Orders(setup.store.redis).stream("remote"))


def _ordered(setup, monkeypatch) -> list:  # noqa: F811
    from scripts.swarm_v2.runtime.base import Outcome, Status

    sent = []

    def send(request, selected, task):
        sent.append(selected.hive)
        return Outcome("spawn", Status.OK, "hive", None)

    monkeypatch.setattr(setup.launcher, "send_order", send)
    return sent


def test_a_seat_placed_on_another_hive_is_refused_while_a_user_bundle_is_registered(setup, tmp_path, monkeypatch):  # noqa: F811
    from scripts.hive import launch
    from scripts.swarm_v2.runtime.base import Status

    user = tmp_path / "user-bundle"
    user.mkdir()
    _register(user)
    sent = _ordered(setup, monkeypatch)
    reserved = _unreserved(setup, monkeypatch)

    outcome = setup.launcher.spawn(_request(setup))

    reserved.assert_not_called()
    assert outcome.status is Status.REFUSED
    assert outcome.backend == launch.HIVE
    assert outcome.detail == (
        f"hive remote cannot run this seat: the user bundle at {user} is registered on this host and a remote "
        "hive carries only the root bundle; run agentihooks bundle unlink --user to place it remotely"
    )
    assert sent == []
    assert _published(setup) == 0
    assert setup.launcher.orders_for("task") is None


def test_a_registered_user_bundle_whose_directory_is_gone_still_refuses_the_remote_placement(
    setup,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from scripts.swarm_v2.runtime.base import Status

    _register(tmp_path / "moved-away")
    sent = _ordered(setup, monkeypatch)
    reserved = _unreserved(setup, monkeypatch)

    outcome = setup.launcher.spawn(_request(setup))

    reserved.assert_not_called()
    assert outcome.status is Status.REFUSED
    assert "moved-away" in outcome.detail
    assert sent == []
    assert _published(setup) == 0
    assert setup.launcher.orders_for("task") is None


def test_a_seat_placed_on_the_launching_hive_is_not_refused(setup, tmp_path, monkeypatch):  # noqa: F811
    user = tmp_path / "user-bundle"
    user.mkdir()
    _register(user)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "remote")
    sent = _ordered(setup, monkeypatch)

    outcome = setup.launcher.spawn(_request(setup))

    assert outcome.ok
    assert sent == ["remote"]


def test_a_seat_placed_on_another_hive_without_a_user_bundle_is_ordered_as_before(setup, monkeypatch):  # noqa: F811
    sent = _ordered(setup, monkeypatch)

    outcome = setup.launcher.spawn(_request(setup))

    assert outcome.ok
    assert sent == ["remote"]


def test_an_order_published_before_the_registration_is_answered_and_never_revoked(setup, tmp_path):  # noqa: F811
    from dataclasses import replace
    from unittest.mock import Mock

    from scripts.hive.orders import Ack
    from tests.hive.test_controller_launch import order_for, request_for

    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    agent = setup.store.execution_registry.get("fixture", order.execution_id)
    setup.store.execution_registry.update("fixture", replace(agent, state="working"))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.launcher.orders.finish("remote", "0-0", order, Ack("live", agent.runtime_target))
    user = tmp_path / "user-bundle"
    user.mkdir()
    _register(user)
    setup.launcher.wait = Mock(return_value="pending")

    assert setup.launcher.spawn(request) == "pending"

    assert setup.launcher.orders_for("task") == order
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "working"
    assert setup.store.redis.hget(setup.store.key("fixture", "launch-endings"), order.execution_id) is None


def test_an_environment_only_registration_refuses_the_remote_placement_before_any_reservation_or_order(
    setup,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from scripts.hive import launch
    from scripts.swarm_v2.runtime.base import Status

    user = tmp_path / "from-env"
    user.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_USER_BUNDLE_PATH", str(user))
    reserved = _unreserved(setup, monkeypatch)

    outcome = setup.launcher.spawn(_request(setup))

    reserved.assert_not_called()
    assert outcome.status is Status.REFUSED
    assert outcome.backend == launch.HIVE
    assert f"the user bundle at {user} is registered on this host" in outcome.detail
    assert _published(setup) == 0
    assert setup.launcher.orders_for("task") is None


def test_an_environment_only_registration_with_an_absent_path_still_refuses_the_remote_placement(
    setup,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from scripts.swarm_v2.runtime.base import Status

    absent = tmp_path / "from-env-absent"
    monkeypatch.setenv("AGENTIHOOKS_USER_BUNDLE_PATH", str(absent))
    reserved = _unreserved(setup, monkeypatch)

    outcome = setup.launcher.spawn(_request(setup))

    reserved.assert_not_called()
    assert outcome.status is Status.REFUSED
    assert f"the user bundle at {absent} is registered on this host" in outcome.detail
    assert _published(setup) == 0
    assert setup.launcher.orders_for("task") is None


def test_a_state_registration_refuses_without_publishing_when_the_order_path_is_left_real(
    setup,  # noqa: F811
    tmp_path,
):
    from scripts.swarm_v2.runtime.base import Status

    user = tmp_path / "user-bundle"
    user.mkdir()
    _register(user)

    outcome = setup.launcher.spawn(_request(setup))

    assert outcome.status is Status.REFUSED
    assert _published(setup) == 0
    assert setup.launcher.orders_for("task") is None
