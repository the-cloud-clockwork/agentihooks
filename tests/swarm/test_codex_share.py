"""Codex takes at least its share of new eng and ci spawns while a Codex seat is open."""

import pytest

from scripts.swarm import capacity
from scripts.swarm.store import AgentRecord, SwarmConfig


def account(name: str, harness: str = "claude", cap: int = 6, sessions: int = 0) -> capacity.Account:
    return capacity.Account(harness, name, capacity._state(cap, None), sessions, 90, 90, cap)


def live(lane: str, harness: str, count: int, state: str = "working") -> list[AgentRecord]:
    return [AgentRecord(f"{lane}-{harness}-{state}-{n}", lane, "", harness=harness, state=state) for n in range(count)]


def harnesses(decision: dict, lane: str) -> list[str]:
    return [slot["harness"] for slot in decision["placements"][lane]]


@pytest.mark.feature("codex-swarm-share")
def test_codex_takes_thirty_percent_of_new_eng_spawns_beside_four_claude_accounts() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=10, max_ci=0, max_plan=0)
    rows = [account(name) for name in ("a", "b", "c", "d")] + [account("cx", "codex")]
    decision = capacity.calculate(config, rows, [])
    assert [(slot["harness"], slot["account"]) for slot in decision["placements"]["eng"]] == [
        ("codex", "cx"),
        ("claude", "a"),
        ("claude", "b"),
        ("codex", "cx"),
        ("claude", "c"),
        ("claude", "d"),
        ("codex", "cx"),
        ("claude", "a"),
        ("claude", "b"),
        ("claude", "c"),
    ]


@pytest.mark.feature("codex-swarm-share")
def test_the_share_counts_live_eng_and_ci_agents_and_nothing_else() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=8, max_ci=0, max_plan=0)
    agents = (
        live("eng", "claude", 5)
        + live("ci", "claude", 2)
        + live("eng", "codex", 2)
        + live("ci", "codex", 1)
        + live("eng", "codex", 1, state="finished")
        + live("plan", "codex", 1)
    )
    decision = capacity.calculate(config, [account("a"), account("cx", "codex")], agents)
    assert harnesses(decision, "eng") == ["codex"]


@pytest.mark.feature("codex-swarm-share")
def test_a_met_share_leaves_the_fewest_sessions_pick() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=8, max_ci=0, max_plan=0)
    agents = live("eng", "claude", 5) + live("ci", "claude", 1) + live("eng", "codex", 2) + live("ci", "codex", 1)
    decision = capacity.calculate(config, [account("a"), account("cx", "codex")], agents)
    assert harnesses(decision, "eng") == ["claude"]


@pytest.mark.feature("codex-swarm-share")
def test_a_short_share_with_two_codex_of_seven_sends_the_next_spawn_to_codex() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=7, max_ci=0, max_plan=0)
    agents = live("eng", "claude", 4) + live("eng", "codex", 2)
    decision = capacity.calculate(config, [account("a"), account("cx", "codex")], agents)
    assert harnesses(decision, "eng") == ["codex"]


@pytest.mark.feature("codex-swarm-share")
def test_ci_spawns_share_with_codex_too() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=0, max_ci=1, max_plan=0)
    decision = capacity.calculate(config, [account("a"), account("cx", "codex")], [])
    assert harnesses(decision, "ci") == ["codex"]


@pytest.mark.feature("codex-swarm-share")
def test_the_plan_lane_keeps_the_fewest_sessions_pick() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, max_plan=1)
    decision = capacity.calculate(config, [account("a"), account("cx", "codex")], [])
    assert harnesses(decision, "plan") == ["claude"]


@pytest.mark.feature("codex-swarm-share")
@pytest.mark.parametrize("codex", [account("cx", "codex", cap=0), account("cx", "codex", cap=2, sessions=2)])
def test_a_short_share_falls_back_to_claude_without_a_codex_seat(codex: capacity.Account) -> None:
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    decision = capacity.calculate(config, [account("a"), codex], [])
    assert harnesses(decision, "eng") == ["claude"]


@pytest.mark.feature("codex-swarm-share")
def test_a_claude_lane_pin_wins_over_a_short_share() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, lanes={"eng": {"agent": "claude"}})
    decision = capacity.calculate(config, [account("a"), account("cx", "codex")], [])
    assert harnesses(decision, "eng") == ["claude"]


@pytest.mark.feature("codex-swarm-share")
def test_a_claude_task_requirement_wins_over_a_short_share() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    requirements = {"eng": [("claude",)], "ci": [], "plan": []}
    decision = capacity.calculate(
        config, [account("a"), account("cx", "codex")], [], {"eng": 1, "ci": 0, "plan": 0}, requirements
    )
    assert harnesses(decision, "eng") == ["claude"]


@pytest.mark.feature("codex-swarm-share")
def test_a_codex_plan_spawn_never_counts_toward_the_eng_share() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=0, max_plan=1)
    agents = live("eng", "claude", 2) + live("eng", "codex", 1)
    decision = capacity.calculate(config, [account("a", sessions=1), account("cx", "codex")], agents)
    assert (harnesses(decision, "plan"), harnesses(decision, "eng")) == (["codex"], ["codex"])


@pytest.mark.feature("codex-swarm-share")
def test_a_short_share_leaves_a_codex_seat_reserved_for_pinned_codex_work() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=0, max_plan=0)
    requirements = {"eng": [("claude", "codex"), ("claude",), ("codex",)], "ci": [], "plan": []}
    rows = [account("a", cap=1), account("cx", "codex", cap=1)]
    decision = capacity.calculate(config, rows, [], {"eng": 3, "ci": 0, "plan": 0}, requirements)
    assert [(slot["index"], slot["harness"]) for slot in decision["placements"]["eng"]] == [(0, "claude"), (2, "codex")]


@pytest.mark.feature("codex-swarm-share")
def test_a_met_share_still_leaves_a_claude_seat_reserved_for_pinned_claude_work() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=0, max_plan=0)
    requirements = {"eng": [("claude", "codex"), ("claude",)], "ci": [], "plan": []}
    rows = [account("a", cap=2, sessions=1), account("cx", "codex", cap=2, sessions=1)]
    agents = live("eng", "claude", 1) + live("eng", "codex", 1)
    decision = capacity.calculate(config, rows, agents, {"eng": 2, "ci": 0, "plan": 0}, requirements)
    assert [(slot["index"], slot["harness"]) for slot in decision["placements"]["eng"]] == [(0, "codex"), (1, "claude")]
