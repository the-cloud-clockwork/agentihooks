"""The expert agent kind: its name and seat, its profile, where it places, how its seat mail moves and the launch
the runtime holds it to."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.inbox import exits
from scripts.inbox.seats import is_seat
from scripts.inbox.store import InboxStore
from scripts.swarm import effort_range, naming
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm.tick import SpawnError
from scripts.swarm_v2.runtime.base import HIVE_LANES, Placement
from tests.swarm import profile_fixture

pytestmark = pytest.mark.unit

NAME = "expert@a1b2c3-0001"
SEAT = "expert-1@sw"
PINS: dict[str, Any] = {"profile": "expert", "requires_harness": "claude"}
TASK: dict[str, Any] = {"id": "expert-1", "title": "Mission", "seat": SEAT, "hive_opening": "Work the mission.", **PINS}
STARTED = "status=started\nroute_status=routed\npane_id=w2:p9\naccount=a1\n"


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    found.create(SwarmConfig("sw", "agentihooks", 1, 0))
    return found


def _config(tmp_path: Path, **settings: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {"lanes": {}, **settings}
    return SimpleNamespace(slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, autonomy="delegate", **fields)


def _started(argv: list[str]) -> SimpleNamespace:
    out: str = profile_fixture.validated(argv, STARTED)
    return SimpleNamespace(returncode=0, stdout=out, stderr="")


def _passed(argv: list[str]) -> list[str]:
    return argv[argv.index("--") + 1 :]


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task: dict[str, Any],
    env: dict[str, str] | None = None,
    **settings: Any,
) -> dict[str, Any]:
    for agent in ("CLAUDE", "CODEX"):
        for key in ("MODEL", "EFFORT"):
            monkeypatch.delenv(f"AGENTIHOOKS_{agent}_{key}", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.update(argv=argv, env=kwargs["env"])
        return _started(argv)

    def choose(requested: str, environ: dict[str, str]) -> tuple[str, str]:
        return requested or "codex", "requested" if requested else "open"

    HerdrRuntime(home=tmp_path, run=run, choose=choose).spawn(_config(tmp_path, **settings), "expert", NAME, task)
    return seen


@pytest.mark.feature("expert-kind-and-launch-pin")
def test_an_expert_name_builds_parses_and_counts_on_its_own(store: RedisStore) -> None:
    code = store.config("sw").code
    names = [store.next_name("sw", lane) for lane in ("expert", "eng", "expert")]
    assert names == [f"expert@{code}-0001", f"engineer@{code}-0001", f"expert@{code}-0002"]
    parsed = naming.parse(names[0])
    assert parsed is not None
    assert (parsed.kind, parsed.code, parsed.number, parsed.lane) == ("expert", code, 1, "expert")
    assert naming.lane_of(names[2]) == "expert"
    assert naming.build("expert", "a1b2c3", 12) == "expert@a1b2c3-0012"
    assert naming.PATTERNS["pane"].fullmatch(naming.plain(NAME))


def test_an_expert_seat_is_numbered_from_one_and_is_a_seat_address() -> None:
    assert [naming.seat_label("expert", index) for index in (1, 2, 17)] == ["expert-1", "expert-2", "expert-17"]
    assert is_seat(SEAT)
    assert not is_seat(NAME)


def test_the_expert_lane_mounts_the_expert_profile_when_its_task_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DEFAULT_PROFILES["expert"] == "expert"
    task = {key: value for key, value in TASK.items() if key != "profile"}
    assert _flag(_launch(tmp_path, monkeypatch, task)["argv"], "--profile") == "expert"


@pytest.mark.parametrize("configured", [("low", "medium"), ("medium", "xhigh"), ("max", "max")])
def test_the_expert_launch_range_is_xhigh_to_max_whatever_the_swarm_range(configured: tuple[str, str]) -> None:
    config = SwarmConfig("sw", "/repo", 0, 0, effort_min=configured[0], effort_max=configured[1])
    assert effort_range.bounds(config, "expert") == ("xhigh", "max")


@pytest.mark.feature("expert-kind-and-launch-pin")
def test_a_fresh_expert_launch_runs_claude_code_on_fable_at_xhigh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _launch(tmp_path, monkeypatch, TASK)["argv"]
    assert (_flag(argv, "--agent"), _flag(argv, "--profile")) == ("claude", "expert")
    assert _passed(argv) == ["--model", "fable", "--effort", "xhigh"]


@pytest.mark.feature("expert-kind-and-launch-pin")
@pytest.mark.parametrize(
    "wanted,swarm_range,launched",
    [
        ("max", ("medium", "xhigh"), "max"),
        ("max", ("low", "medium"), "max"),
        ("xhigh", ("max", "max"), "xhigh"),
        ("", ("max", "max"), "xhigh"),
        ("high", ("low", "high"), "xhigh"),
    ],
)
def test_the_lineage_effort_decides_the_launch_effort_whatever_the_swarm_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wanted: str, swarm_range: tuple[str, str], launched: str
) -> None:
    task = {**TASK, "expert_effort": wanted}
    seen = _launch(tmp_path, monkeypatch, task, effort_min=swarm_range[0], effort_max=swarm_range[1])
    assert _passed(seen["argv"]) == ["--model", "fable", "--effort", launched]
    assert seen["env"]["AGENTIHOOKS_SWARM_EFFORT_RANGE"] == "xhigh:max"


def test_a_lane_setting_or_an_environment_default_never_moves_an_expert_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"AGENTIHOOKS_CLAUDE_MODEL": "opus", "AGENTIHOOKS_CLAUDE_EFFORT": "low"}
    lanes = {"expert": {"model": "sonnet", "effort": "medium"}}
    seen = _launch(tmp_path, monkeypatch, {**TASK, "expert_effort": "max"}, env=env, lanes=lanes)
    assert _passed(seen["argv"]) == ["--model", "fable", "--effort", "max"]


@pytest.mark.feature("expert-kind-and-launch-pin")
@pytest.mark.parametrize(
    "saved_model,saved_effort,wanted,launched",
    [("opus", "medium", "max", "max"), ("sonnet", "max", "", "xhigh"), ("fable", "low", "xhigh", "xhigh")],
)
def test_a_successor_launches_fable_at_the_lineage_effort_whatever_its_predecessor_ran(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved_model: str,
    saved_effort: str,
    wanted: str,
    launched: str,
) -> None:
    saved = {"profile": "expert", "harness": "claude", "model": saved_model, "effort": saved_effort}
    envelope = {"reason": "recycle", "launch": saved}
    task = {**TASK, "expert_effort": wanted, "handoff": "# Expert handoff", "handoff_envelope": envelope}
    argv = _launch(tmp_path, monkeypatch, task)["argv"]
    assert _flag(argv, "--agent") == "claude"
    assert _passed(argv) == ["--model", "fable", "--effort", launched]


def test_a_quota_transfer_with_no_saved_launch_still_launches_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope: dict[str, Any] = {"reason": "quota", "launch": {}}
    task = {**TASK, "expert_effort": "max", "handoff": "# Expert handoff", "handoff_envelope": envelope}
    argv = _launch(tmp_path, monkeypatch, task)["argv"]
    assert _flag(argv, "--agent") == "claude"
    assert _passed(argv) == ["--model", "fable", "--effort", "max"]


@pytest.mark.feature("expert-kind-and-launch-pin")
@pytest.mark.parametrize("pin", [{"requires_harness": "codex"}, {"requires_harness": ""}])
def test_an_expert_launch_that_lands_on_another_harness_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin: dict[str, str]
) -> None:
    with pytest.raises(SpawnError) as refused:
        _launch(tmp_path, monkeypatch, {**TASK, **pin})
    assert str(refused.value) == "an expert launches on claude, never on codex"
    assert refused.value.status == "unsupported"


@pytest.mark.feature("expert-kind-and-launch-pin")
@pytest.mark.parametrize(
    "recorded_model,recorded_effort,launched",
    [("fable", "max", "max"), ("opus", "medium", "xhigh"), ("claude-fable-5-2", "xhigh", "xhigh")],
)
def test_a_resumed_expert_relaunches_fable_inside_its_pin(
    tmp_path: Path, recorded_model: str, recorded_effort: str, launched: str
) -> None:
    runs: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        runs.append(argv)
        return _started(argv)

    def herdr(args: list[str]) -> dict[str, Any]:
        session = {"kind": "id", "value": "c0ffee"}
        listed = {"name": "w2:p9", "pane_id": "w2:p9", "agent": "claude", "agent_status": "working"}
        return {"agents": [{**listed, "agent_session": session}]}

    runtime = HerdrRuntime(home=tmp_path, run=run, herdr=herdr, choose=lambda *_: ("claude", "open"))
    runtime.sleep = lambda seconds: None
    agent = AgentRecord(
        NAME,
        "expert",
        "expert-1",
        harness="claude",
        account="a1",
        model=recorded_model,
        effort=recorded_effort,
        profile="expert",
        seat=SEAT,
        conversation_id="c0ffee",
    )
    runtime.resume(_config(tmp_path), agent, "you were restored")
    assert _passed(runs[0]) == ["--route", "a1", "--model", "fable", "--effort", launched]


def test_an_expert_task_places_on_the_hive_backend() -> None:
    assert "expert" in HIVE_LANES
    placement = Placement("hive", lanes=HIVE_LANES, local_profiles=frozenset())
    assert placement.backend_for("expert", TASK) == "hive"
    assert placement.backend_for("expert", {**TASK, "workstation": True}) == "local"


def test_expert_seat_mail_moves_to_the_host_of_the_seats_next_occupant(store: RedisStore) -> None:
    from scripts.hive import auth

    for hive in ("m1", "m2"):
        store.redis.hset(f"{auth.PREFIX}:member:{hive}", mapping={"name": hive, "ledger": "digest"})
        store.redis.sadd(auth.INDEX, hive)
    first = store.next_name("sw", "expert")
    store.start_execution("sw", AgentRecord(first, "expert", "expert-1", seat=SEAT, hive="m1"))
    store.seats.occupy(SEAT, first, 1)
    item = InboxStore(store.redis).send("sender", first, "contract")
    member = InboxStore(store.redis, world="m1")
    assert [found.id for found in member.inbox(first)] == [item.id]
    agent = next(found for found in store.agents("sw") if found.name == first)
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, dict)
    second = store.next_name("sw", "expert")
    store.start_execution("sw", AgentRecord(second, "expert", "expert-1", seat=SEAT, hive="m2"), agent.execution_id)
    store.seats.occupy(SEAT, second, 2)
    exits.sweep(InboxStore(store.redis), "sw", store, dict)
    moved = InboxStore(store.redis, world="m2")
    assert [found.id for found in moved.open_mail(second)] == [item.id]
    assert member.inbox(SEAT) == []
    assert moved.history(item.id)[-1]["reason"] == f"moved to hive m2, where {second} runs {SEAT}"
