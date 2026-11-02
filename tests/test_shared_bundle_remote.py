import json
from dataclasses import replace

from scripts import install
from scripts.swarm.store import SwarmConfig
from scripts.swarm_v2.runtime.base import Placement, RuntimeRouter, SpawnRequest, Status
from tests.test_swarm_v2_launch import BACKEND, FIRST, OTHER_API, SLUG, pod_target, world  # noqa: F401


def _register(**linked) -> None:
    state = json.loads(install.STATE_JSON.read_text()) if install.STATE_JSON.exists() else {}
    install.STATE_JSON.write_text(json.dumps({**state, **{key: {"path": str(path)} for key, path in linked.items()}}))


def _tick_spawn(world, profile: str = "engineer"):  # noqa: F811
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API)
    request = SpawnRequest(
        config, "eng", world.store.next_name(SLUG, "eng"), {"id": "task", "seat": FIRST, "profile": profile}
    )
    world.launcher.router = RuntimeRouter([world.runtime], placement=Placement(BACKEND))
    return world.launcher.from_tick(request, replace(world.terms, bundled=True), pod_target)


def test_a_registered_shared_bundle_never_refuses_a_remote_launch(world, tmp_path):  # noqa: F811
    shared = tmp_path / "shared-bundle"
    shared.mkdir()
    _register(shared_bundle=shared)

    outcome = _tick_spawn(world)

    assert outcome.status is Status.OK
    [sent] = world.runtime.requests
    assert world.store.execution(SLUG, sent.task["execution_id"]).profile == "engineer"


def test_a_shared_bundle_whose_directory_is_gone_does_not_refuse_the_remote_launch(world, tmp_path):  # noqa: F811
    _register(shared_bundle=tmp_path / "moved-away")

    assert _tick_spawn(world).status is Status.OK


def test_the_environment_registering_a_shared_bundle_does_not_refuse_the_remote_launch(world, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_SHARED_BUNDLE_PATH", str(tmp_path / "from-env"))

    assert _tick_spawn(world).status is Status.OK


def test_a_user_bundle_still_refuses_beside_a_shared_one_and_names_only_the_user_bundle(world, tmp_path):  # noqa: F811
    shared, user = tmp_path / "shared-bundle", tmp_path / "user-bundle"
    shared.mkdir()
    user.mkdir()
    _register(shared_bundle=shared, user_bundle=user)

    outcome = _tick_spawn(world)

    assert outcome.status is Status.REFUSED
    assert outcome.detail == (
        f"profile engineer cannot run on a worker pod: the user bundle at {user} is registered on this host and a "
        "pod carries only the root bundle; run agentihooks bundle unlink --user to launch remotely"
    )
    assert world.runtime.requests == []
