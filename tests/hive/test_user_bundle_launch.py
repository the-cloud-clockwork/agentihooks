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

    outcome = setup.launcher.spawn(_request(setup))

    assert outcome.status is Status.REFUSED
    assert outcome.backend == launch.HIVE
    assert outcome.detail == (
        f"hive remote cannot run this seat: the user bundle at {user} is registered on this host and a remote "
        "hive carries only the root bundle; run agentihooks bundle unlink --user to place it remotely"
    )
    assert sent == []


def test_a_registered_user_bundle_whose_directory_is_gone_still_refuses_the_remote_placement(
    setup,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from scripts.swarm_v2.runtime.base import Status

    _register(tmp_path / "moved-away")
    sent = _ordered(setup, monkeypatch)

    outcome = setup.launcher.spawn(_request(setup))

    assert outcome.status is Status.REFUSED
    assert "moved-away" in outcome.detail
    assert sent == []


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
