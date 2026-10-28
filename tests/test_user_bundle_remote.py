import json
from dataclasses import replace

from scripts import install
from scripts.swarm.store import SwarmConfig
from scripts.swarm_v2.runtime.base import Placement, RuntimeRouter, SpawnRequest, Status
from tests.test_swarm_v2_launch import BACKEND, FIRST, OTHER_API, SLUG, pod_target, world  # noqa: F401


def _register(path) -> None:
    state = json.loads(install.STATE_JSON.read_text()) if install.STATE_JSON.exists() else {}
    install.STATE_JSON.write_text(json.dumps({**state, "user_bundle": {"path": str(path)}}))


def _tick_spawn(world, profile: str = "engineer"):  # noqa: F811
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API)
    request = SpawnRequest(config, "eng", "e1", {"id": "task", "seat": FIRST, "profile": profile})
    world.launcher.router = RuntimeRouter([world.runtime], placement=Placement(BACKEND))
    return world.launcher.from_tick(request, replace(world.terms, bundled=True), pod_target)


def test_a_remote_launch_is_refused_while_a_user_bundle_is_registered_on_the_launching_host(world, tmp_path):  # noqa: F811
    user = tmp_path / "user-bundle"
    user.mkdir()
    _register(user)

    outcome = _tick_spawn(world)

    assert outcome.status is Status.REFUSED
    assert outcome.detail == (
        f"profile engineer cannot run on a worker pod: the user bundle at {user} is registered on this host and a "
        "pod carries only the root bundle; run agentihooks bundle unlink --user to launch remotely"
    )
    assert world.runtime.requests == []
    assert world.rows() == {}


def test_a_registered_user_bundle_whose_directory_is_gone_still_refuses_the_remote_launch(world, tmp_path):  # noqa: F811
    _register(tmp_path / "moved-away")

    outcome = _tick_spawn(world)

    assert outcome.status is Status.REFUSED
    assert "moved-away" in outcome.detail
    assert world.runtime.requests == []


def test_the_environment_registers_a_user_bundle_for_the_remote_refusal(world, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_USER_BUNDLE_PATH", str(tmp_path / "from-env"))

    outcome = _tick_spawn(world)

    assert outcome.status is Status.REFUSED
    assert "from-env" in outcome.detail


def test_a_remote_launch_without_a_user_bundle_is_admitted_as_before(world):  # noqa: F811
    outcome = _tick_spawn(world)

    assert outcome.status is Status.OK
    [sent] = world.runtime.requests
    assert world.store.execution(SLUG, sent.task["execution_id"]).profile == "engineer"


def test_the_user_bundle_refusal_comes_before_the_unshipped_profile_refusal(world, tmp_path):  # noqa: F811
    user = tmp_path / "user-bundle"
    user.mkdir()
    _register(user)

    outcome = _tick_spawn(world, profile="designer")

    assert "user bundle" in outcome.detail
    assert world.runtime.requests == []
