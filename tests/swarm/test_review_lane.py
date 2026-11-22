"""The review lane: reviewer names and seats, the template lane, the swarm config cap, ledger task add and claims."""

from pathlib import Path

import pytest

from scripts.swarm import capacity, cli, naming, quota_view, readiness, templates
from scripts.swarm import tick as swarm_tick
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_creator, ledger_tasks, new_ledger
from scripts.tick.steps import tasks as task_steps
from tests.swarm.test_capacity import _readings, account
from tests.swarm.test_create_refusal import _args as create_args
from tests.swarm.test_readiness import doc as readiness_doc
from tests.swarm.test_readiness import snapshot as readiness_snapshot
from tests.swarm.test_readiness import task as readiness_task
from tests.swarm.test_tick import FakeLedger
from tests.swarm_ledger import legacy_page
from tests.swarm_ledger.test_plan_kind import add

pytestmark = pytest.mark.unit

PAIRING = "^review tasks must use the review lane, and the review lane accepts only review tasks$"


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return found


@pytest.fixture
def review_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Review proof", "overview": "Project intent", "sources": [], "phases": [{"title": "Build"}]}
    html, json_path = legacy_page.paths("review-proof", core)
    json_path.unlink(missing_ok=True)
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), "review-proof", 8765))
    core.sync("review-proof")
    return "review-proof"


def _ready(store: RedisStore, tasks: list[dict]) -> dict[str, list[str]]:
    _, ready = capacity.ready_work("sw", store, FakeLedger(tasks).state("sw"))
    return {lane: [task["id"] for task in found] for lane, found in ready.items() if found}


def _scaled(config: SwarmConfig) -> tuple[SwarmConfig, dict]:
    inputs = capacity.fixture_inputs({**_readings(), "previous": {}})
    inputs = capacity.ScaleInputs(inputs.observations, inputs.agents, inputs.demand, lambda: None, {})
    scaled, decision = capacity.autoscaled(config, inputs)
    assert decision is not None
    return scaled, decision


@pytest.mark.feature("swarm-review-lane")
def test_reviewer_names_round_trip_to_the_review_lane() -> None:
    name = naming.build("reviewer", "abcdef", 1)
    assert name == "reviewer@abcdef-0001"
    parsed = naming.parse(name)
    assert parsed is not None
    assert parsed.lane == "review"
    assert naming.lane_of(name) == "review"


@pytest.mark.feature("swarm-review-lane")
def test_review_seats_are_numbered_like_worker_seats() -> None:
    assert naming.seat_label("eng", 2) == "eng-2"
    assert naming.seat_label("review", 2) == "review-2"
    assert swarm_tick.seat_address("sw", naming.seat_label("review", 2)) == "review-2@sw"


@pytest.mark.feature("swarm-review-lane")
def test_a_template_carries_the_review_lane_with_its_cap_profile_and_kind() -> None:
    template = templates.parse({"name": "review-proof"})
    assert template.lanes["eng"].kind == templates.AUTO
    lane = template.lanes["review"]
    assert (lane.cap, lane.profile, lane.kind) == (1, "reviewer", "review")


@pytest.mark.feature("swarm-review-lane")
def test_a_template_from_a_swarm_config_keeps_each_worker_cap() -> None:
    config = SwarmConfig("caps-proof", "/repo", 4, 2, max_plan=3)
    caps = {key: lane.cap for key, lane in templates.from_config("caps-proof", config).lanes.items()}
    assert (caps["eng"], caps["ci"], caps["plan"], caps["master"]) == (4, 2, 3, 1)


@pytest.mark.feature("swarm-review-lane")
def test_a_template_from_a_swarm_config_carries_the_review_cap() -> None:
    config = SwarmConfig("review-proof", "/repo", 2, 1, max_review=3)
    template = templates.from_config("review-proof", config)
    assert {key: lane.cap for key, lane in template.lanes.items()} == {
        "eng": 2,
        "ci": 1,
        "plan": 1,
        "review": 3,
        "master": 1,
    }


@pytest.mark.feature("swarm-review-lane")
def test_a_new_swarm_takes_the_review_cap_from_its_template(monkeypatch: pytest.MonkeyPatch) -> None:
    template = templates.parse({"name": "wide", "lanes": {"review": {"cap": 3}}})
    monkeypatch.setattr(cli.templates, "load", lambda name, environ: template)
    monkeypatch.setattr("scripts.hive.daemon.origin", lambda path: f"origin of {path}")
    monkeypatch.setattr(ledger_creator, "shared", lambda environ: True)
    assert cli.create_config(create_args()).max_review == 3


@pytest.mark.feature("swarm-review-lane")
def test_existing_swarm_configs_have_no_review_seat(store: RedisStore) -> None:
    assert SwarmConfig("review-proof", "/repo", 2, 1).max_review == 0
    store.redis.hdel(store.key("sw", "config"), "max_review")
    assert store.config("sw").max_review == 0


@pytest.mark.feature("swarm-review-lane")
def test_a_stored_swarm_config_keeps_its_review_cap(store: RedisStore) -> None:
    store.update("sw", max_review=2)
    assert store.config("sw").max_review == 2


@pytest.mark.feature("swarm-review-lane")
def test_capacity_reads_each_worker_lane_cap_from_its_config_field() -> None:
    configured = capacity._configured(SwarmConfig("sw", "/repo", 4, 2, max_plan=3))
    assert (configured["eng"], configured["ci"], configured["plan"]) == (4, 2, 3)


@pytest.mark.feature("swarm-review-lane")
def test_capacity_reads_the_review_cap_from_the_swarm_config() -> None:
    config = SwarmConfig("sw", "/repo", 4, 2, max_plan=3, max_review=2)
    assert capacity._configured(config) == {"eng": 4, "ci": 2, "plan": 3, "review": 2}
    assert capacity.caps({}, config) == {"eng": 4, "ci": 2, "plan": 3, "review": 2}


@pytest.mark.feature("swarm-review-lane")
def test_autoscale_sizes_only_the_engineering_ci_and_plan_lanes() -> None:
    _, decision = _scaled(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto"))
    assert set(decision["ceilings"]) == {"eng", "ci", "plan"}


@pytest.mark.feature("swarm-review-lane")
def test_autoscale_keeps_the_configured_review_cap() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto", max_review=2)
    scaled, decision = _scaled(config)
    assert scaled.max_review == 2
    assert set(decision["ceilings"]) == {"eng", "ci", "plan"}


@pytest.mark.feature("swarm-review-lane")
def test_autoscale_reads_no_demand_as_zero_demand_on_every_scaled_lane() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto", max_review=2)
    inputs = capacity.fixture_inputs({**_readings(), "previous": {}})
    zero = capacity.ScaleInputs(inputs.observations, inputs.agents, {"eng": 0, "ci": 0, "plan": 0}, lambda: None, {})
    none = capacity.ScaleInputs(inputs.observations, inputs.agents, None, lambda: None, {})
    asked = capacity.ScaleInputs(inputs.observations, inputs.agents, {"eng": 1, "ci": 1, "plan": 1}, lambda: None, {})
    _, idle = capacity.autoscaled(config, zero)
    _, unset = capacity.autoscaled(config, none)
    _, wanted = capacity.autoscaled(config, asked)
    assert idle is not None and unset is not None and wanted is not None
    assert unset["ceilings"] == idle["ceilings"]
    assert wanted["ceilings"] != idle["ceilings"]


@pytest.mark.feature("swarm-review-lane")
def test_ready_work_lists_each_task_only_on_its_own_lane(store: RedisStore) -> None:
    assert _ready(store, [{"id": "e1"}, {"id": "c1", "lane": "ci"}]) == {"eng": ["e1"], "ci": ["c1"]}


@pytest.mark.feature("swarm-review-lane")
def test_ready_work_lists_an_open_review_task_on_the_review_lane(store: RedisStore) -> None:
    found = _ready(store, [{"id": "r1", "lane": "review", "kind": "review"}, {"id": "e1"}])
    assert found == {"eng": ["e1"], "review": ["r1"]}


@pytest.mark.feature("swarm-review-lane")
def test_a_drained_swarm_runs_again_for_a_new_review_task(store: RedisStore) -> None:
    store.update("sw", state="drained")
    ledger = FakeLedger([{"id": "r1", "lane": "review", "kind": "review"}])
    state = {
        "slug": "sw",
        "store": store,
        "rows": ledger.rows,
        "doc": ledger.state("sw"),
        "config": store.config("sw"),
        "box": {"sleeping": False},
    }
    assert task_steps._drained_resume(swarm_tick, state.get) == ["new tasks, running again"]
    assert store.config("sw").state == "running"


@pytest.mark.feature("swarm-review-lane")
def test_the_ledger_refuses_a_lane_outside_the_swarm_lanes(review_ledger: str) -> None:
    with pytest.raises(ValueError, match="^lane must be one of"):
        add(review_ledger, "bad", lane="unknown")


@pytest.mark.feature("swarm-review-lane")
def test_the_ledger_stores_a_review_lane_review_task(review_ledger: str) -> None:
    state, rejected = add(review_ledger, "review-1", lane="review", kind="review")
    assert rejected == []
    assert [(task["id"], task["lane"], task["kind"]) for task in state["tasks"]] == [("review-1", "review", "review")]


@pytest.mark.feature("swarm-review-lane")
@pytest.mark.parametrize(("lane", "kind"), [("review", "code"), ("eng", "review"), ("ci", "review")])
def test_the_review_lane_accepts_only_review_tasks(review_ledger: str, lane: str, kind: str) -> None:
    with pytest.raises(ValueError, match=PAIRING):
        add(review_ledger, "bad", lane=lane, kind=kind)


@pytest.mark.feature("swarm-review-lane")
def test_the_task_command_takes_the_review_lane() -> None:
    from scripts.swarm_ledger import ledger

    argv = ["--slug", "demo", "--as", "planner", "task", "add", "review", "Review", "--lane", "review"]
    args = ledger.build_parser().parse_args([*argv, "--kind", "review"])
    assert (args.lane, args.kind) == ("review", "review")


@pytest.mark.feature("swarm-review-lane")
def test_readiness_offers_only_the_lanes_the_event_scope_carries(monkeypatch: pytest.MonkeyPatch) -> None:
    found = readiness_doc([readiness_task("e"), readiness_task("r", lane="review", kind="review")])
    monkeypatch.setattr(readiness, "LANES", ("eng", "ci", "plan"))
    assert [event.key for event in readiness.offers(readiness_snapshot(found), 5_000)] == ["e"]
    monkeypatch.setattr(readiness, "LANES", ("eng", "ci", "plan", "review"))
    assert sorted(event.key for event in readiness.offers(readiness_snapshot(found), 5_000)) == ["e", "r"]


@pytest.mark.feature("swarm-review-lane")
def test_capacity_places_a_reviewer_only_for_review_demand() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, max_review=2)
    rows = [account(cap=6)]
    idle = capacity.calculate(config, rows, [], {"eng": 1, "ci": 0, "plan": 0})
    assert (len(idle["placements"]["eng"]), idle["placements"]["review"]) == (1, [])
    asked = capacity.calculate(config, rows, [], {"eng": 0, "ci": 0, "plan": 0, "review": 1})
    assert [place["index"] for place in asked["placements"]["review"]] == [0]
    narrowed = capacity.calculate(
        config, rows, [], {"eng": 1, "ci": 0, "plan": 0, "review": 1}, {"eng": [("claude",)], "ci": [], "plan": []}
    )
    assert (len(narrowed["placements"]["eng"]), narrowed["placements"]["review"]) == (1, [])


@pytest.mark.feature("swarm-review-lane")
def test_quota_status_reads_a_decision_stored_before_the_review_lane() -> None:
    decision = {
        "configured": {"eng": 4, "ci": 1, "plan": 1},
        "effective": {"eng": 2, "ci": 1, "plan": 0},
        "reason": "accounts are closed",
        "accounts": [],
        "at": 1_000_000,
    }
    head, scaling = quota_view.lines(decision, 1_000_000, {"eng": 2, "ci": 1, "plan": 0, "review": 0}, "manual")
    assert head == "quota capacity eng 2 of 4, ci 1 of 1, plan 0 of 1, changed just now, because accounts are closed"
    assert scaling == "scaling manual, live of ceiling eng 2 of 4, ci 1 of 1, plan 0 of 1"


@pytest.mark.feature("swarm-review-lane")
def test_quota_status_shows_the_configured_review_cap_beside_the_autoscaled_ceilings() -> None:
    decision = {
        "configured": {"eng": 4, "ci": 1, "plan": 1, "review": 2},
        "autoscale": {"ceilings": {"eng": 6, "ci": 2, "plan": 1}, "reason": "Quota seats 3."},
    }
    assert quota_view.scaling_line(decision, {"eng": 2, "ci": 1, "plan": 0}, "auto") == (
        "scaling auto, live of ceiling eng 2 of 6, ci 1 of 2, plan 0 of 1, review 0 of 2, because Quota seats 3"
    )
