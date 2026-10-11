from functools import partial
from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm import capacity, profile_choice, runtime
from scripts.swarm.store import SwarmConfig
from scripts.swarm.tick import SpawnError
from scripts.swarm_v2 import launch
from scripts.swarm_v2.runtime.base import SpawnRequest, Status
from scripts.swarm_v2.runtime.routed import routed
from tests.swarm.profile_fixture import validated
from tests.test_swarm_v2_launch import FIRST, OTHER_API, SLUG, Herdr, World, pod_target

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

TASK = {"id": "t1", "title": "Restyle the ledger page header"}
SHIPPED = launch.worker_profiles
REMEDY = (
    f"set it with agentihooks ledger --slug {SLUG} task set t1 profile=<frontend|engineer|qa>, "
    "or split the task into one public responsibility each, then reopen it"
)


@pytest.fixture(autouse=True)
def frontend_shipped(monkeypatch):
    monkeypatch.setattr(launch, "worker_profiles", lambda: {"default", "engineer", "frontend", "qa"})
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda profile: False)


@pytest.fixture
def asked(monkeypatch):
    tasks = []

    def frontend(state, questions, **kwargs):
        tasks.append(state["task"])
        return DecisionResult(
            {"responsibility": Answer("choice", choice="frontend", confidence=0.9)}, "unit-test", calibrated=True
        )

    monkeypatch.setattr(profile_choice, "decide", frontend)
    return tasks


def local(tmp_path, task, lanes):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    herdr = runtime.HerdrRuntime(home=tmp_path, run=run)
    herdr._quota_accounts = [capacity.Account("claude", "cc", "OPEN", 0, 90, 90, 6)]
    config = SimpleNamespace(
        slug=SLUG, repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes=lanes, autonomy="delegate"
    )
    herdr.spawn(config, "eng", "engineer@a1b2c3-0001", dict(task))
    return seen["argv"][seen["argv"].index("--profile") + 1]


def pod(world, task, lanes):
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API, lanes=lanes)
    request = SpawnRequest(config, "eng", world.store.next_name(SLUG, "eng"), {**task, "seat": FIRST})
    world.launcher.from_tick(request, world.terms, pod_target)
    [sent] = world.runtime.requests
    return world.store.execution(SLUG, sent.task["execution_id"]).profile


def test_a_task_the_classifier_sends_to_frontend_gets_frontend_on_both_paths(tmp_path, monkeypatch, asked):
    on_local = local(tmp_path, TASK, {})
    on_pod = pod(World(monkeypatch), TASK, {})

    assert (on_local, on_pod) == ("frontend", "frontend")
    assert asked == ["t1", "t1"]


def test_a_pod_launch_refuses_a_classifier_profile_the_worker_image_does_not_ship(monkeypatch, asked):
    monkeypatch.setattr(launch, "worker_profiles", SHIPPED)
    world = World(monkeypatch)
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API)
    request = SpawnRequest(config, "eng", world.store.next_name(SLUG, "eng"), {**TASK, "seat": FIRST})

    outcome = world.launcher.from_tick(request, world.terms, pod_target)

    assert (outcome.status, outcome.detail) == (Status.REFUSED, "profile frontend is not shipped in the worker image")
    assert asked == ["t1"]
    assert world.runtime.requests == []


@pytest.mark.parametrize(
    ("task", "lanes"),
    [({**TASK, "profile": "qa"}, {}), (TASK, {"eng": {"profile": "qa"}})],
    ids=["task", "lane"],
)
def test_a_pinned_profile_wins_on_both_paths_without_the_classifier(tmp_path, monkeypatch, asked, task, lanes):
    on_local = local(tmp_path, task, lanes)
    on_pod = pod(World(monkeypatch), task, lanes)

    assert (on_local, on_pod) == ("qa", "qa")
    assert asked == []


def test_a_pod_launch_the_classifier_cannot_place_is_unresolved_before_admission(monkeypatch):
    def down(*args, **kwargs):
        raise ClassifierUnavailable("down")

    monkeypatch.setattr(profile_choice, "decide", down)
    world = World(monkeypatch)

    with pytest.raises(profile_choice.ProfileUnresolved) as unresolved:
        pod(world, TASK, {})

    assert str(unresolved.value) == f"task t1 profile classification is unavailable (down): {REMEDY}"
    assert world.runtime.requests == []
    assert world.rows() == {}


def test_the_tick_runtime_hands_the_tick_an_unresolved_pod_profile(monkeypatch):
    def down(*args, **kwargs):
        raise ClassifierUnavailable("down")

    monkeypatch.setattr(profile_choice, "decide", down)
    world = World(monkeypatch)
    tick_runtime = routed(
        {},
        Herdr(),
        kubernetes=world.runtime,
        launch=partial(world.launcher.from_tick, terms=world.terms, target=pod_target),
    )
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API)

    with pytest.raises(profile_choice.ProfileUnresolved) as unresolved:
        tick_runtime.spawn(config, "eng", world.store.next_name(SLUG, "eng"), {**TASK, "seat": FIRST})

    assert str(unresolved.value) == f"task t1 profile classification is unavailable (down): {REMEDY}"
    assert world.runtime.requests == []


def test_the_shared_pick_decides_without_the_local_install_or_overlays(monkeypatch, asked):
    monkeypatch.setattr(profile_choice, "installed", lambda name: False)

    decision = profile_choice.pick(SLUG, "eng", {}, TASK, {})

    assert (decision.profile, decision.source, decision.overlays) == ("frontend", "classifier", ())


def refuse_classifier(*args, **kwargs):
    pytest.fail("a kept seat profile asked the classifier")


LAUNCH = {"profile": "qa", "harness": "claude", "model": "opus", "effort": "high"}


@pytest.mark.parametrize(
    "task",
    [
        {**TASK, "profile": "engineer", "launch_assignment": LAUNCH},
        {**TASK, "handoff": True, "handoff_envelope": {"launch": LAUNCH}},
    ],
    ids=["relaunch", "handoff"],
)
def test_a_pod_launch_keeps_the_saved_seat_profile_without_the_classifier(monkeypatch, task):
    monkeypatch.setattr(profile_choice, "decide", refuse_classifier)

    assert pod(World(monkeypatch), task, {}) == "qa"


@pytest.mark.parametrize(
    ("task", "profile"),
    [
        ({**TASK, "profile": "engineer", "handoff_envelope": {"launch": LAUNCH}}, "engineer"),
        ({**TASK, "launch_assignment": {**LAUNCH, "effort": ""}}, "frontend"),
    ],
    ids=["task-over-handoff", "incomplete-relaunch"],
)
def test_a_pod_launch_without_a_kept_profile_picks_afresh(monkeypatch, asked, task, profile):
    assert pod(World(monkeypatch), task, {}) == profile


@pytest.mark.parametrize(
    ("task", "reason"),
    [
        ({**TASK, "handoff": True}, "unsupported handoff: original profile and run options are missing"),
        (
            {**TASK, "handoff_envelope": {"launch": {**LAUNCH, "profile": ""}}},
            "unsupported handoff: original profile and run options are missing",
        ),
        (
            {**TASK, "handoff_envelope": {"launch": {**LAUNCH, "harness": "gemini"}}},
            "unsupported handoff harness: gemini",
        ),
    ],
    ids=["handoff-without-launch", "handoff-without-profile", "handoff-on-another-harness"],
)
def test_a_pod_launch_refuses_an_incomplete_handoff_like_the_local_launch(tmp_path, monkeypatch, asked, task, reason):
    with pytest.raises(SpawnError) as on_local:
        local(tmp_path, task, {})
    world = World(monkeypatch)
    with pytest.raises(SpawnError) as on_pod:
        pod(world, task, {})

    assert (str(on_local.value), on_local.value.status) == (reason, "unsupported")
    assert (str(on_pod.value), on_pod.value.status) == (reason, "unsupported")
    assert world.runtime.requests == []
    assert asked == []


def test_a_pod_launch_reads_the_classifier_floor_from_its_environment(monkeypatch, asked):
    monkeypatch.setenv("AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE", "0.95")

    assert pod(World(monkeypatch), TASK, {}) == "engineer"
    assert asked == ["t1"]
