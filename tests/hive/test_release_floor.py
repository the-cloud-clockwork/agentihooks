"""A hive older than the first release of a role does not host it: placement reads the release each hive reports,
judges only a release it can read, and names the release hosting needs when no live hive takes the role."""

from typing import Any

import pytest

from scripts.hive import launch, placement
from scripts.swarm import naming
from scripts.swarm.profile_choice import ProfileDecision
from tests.hive.test_controller_launch import setup, write_hive
from tests.hive.test_excluded_placement import headed, headless
from tests.hive.test_placement import NOW, REPO

pytestmark = pytest.mark.unit
_fixtures = (setup,)

FLOOR = "2.17.1181"
OLD = "2.17.1161"
DEVELOPMENT = "2.17.1.dev5871+g5c6db6a99"
PASSED_OVER = f"it runs release {OLD}, older than {FLOOR}, the first release that hosts expert"


def chosen(hives: list[dict[str, Any]], role: str = "expert") -> placement.Choice | placement.Wait:
    return placement.choose(role, "claude", "acct", "subscription", REPO, hives, NOW)


@pytest.mark.feature("hive-role-release-floor")
@pytest.mark.parametrize(
    ("version", "hosted"),
    [(OLD, False), ("2.17.1180", False), (FLOOR, True), ("2.17.1188", True), ("2.18.0", True)],
)
def test_an_expert_is_hosted_only_by_a_hive_at_or_past_its_first_release(version: str, hosted: bool) -> None:
    assert placement.FIRST_RELEASE["expert"] == FLOOR
    assert placement.hosts("expert", headless(version=version)) is hosted
    assert placement.hosts("expert", headless(version=version), ("expert",)) is hosted


@pytest.mark.feature("hive-role-release-floor")
@pytest.mark.parametrize("version", ["", "unknown", "not a release", DEVELOPMENT, "2.17.1161.dev3"])
def test_a_hive_whose_release_cannot_be_judged_still_hosts_an_expert(version: str) -> None:
    assert placement.release(version) is None
    assert placement.hosts("expert", headless(version=version))


@pytest.mark.feature("hive-role-release-floor")
def test_a_release_a_hive_reports_is_read_as_one_that_can_be_compared() -> None:
    old, floor = placement.release(OLD), placement.release(FLOOR)
    assert old is not None and floor is not None and old < floor
    assert placement.release(None) is None


@pytest.mark.feature("hive-role-release-floor")
def test_a_pinned_hive_is_held_to_the_first_release_of_a_role_too() -> None:
    assert not placement.hosts("expert", headless(version=OLD, pinned="yes"))
    assert placement.hosts("expert", headless(version=FLOOR, pinned="yes"))


@pytest.mark.feature("hive-role-release-floor")
@pytest.mark.parametrize(
    ("role", "aliases"),
    [("eng", ("engineer",)), ("ci", ("cicd",)), ("plan", ("planner",)), ("dispatch", ()), ("master", ())],
)
def test_a_role_with_no_first_release_is_hosted_by_a_hive_of_any_release(role: str, aliases: tuple[str, ...]) -> None:
    assert placement.hosts(role, headed(version=OLD, excluded=[]), aliases)
    assert placement.outdated(role, headed(version=OLD, excluded=[]), aliases) == ""


@pytest.mark.feature("hive-role-release-floor")
def test_the_first_release_a_hive_misses_is_named_and_none_when_it_carries_it() -> None:
    assert placement.outdated("expert", headless(version=OLD)) == FLOOR
    assert placement.outdated("eng", headless(version=OLD), ("expert",)) == FLOOR
    assert placement.outdated("expert", headless(version=FLOOR)) == ""
    assert placement.outdated("expert", headless(version=DEVELOPMENT)) == ""


@pytest.mark.feature("hive-role-release-floor")
def test_a_role_no_live_hive_is_new_enough_for_waits_as_a_role_no_live_hive_takes() -> None:
    assert chosen([headless(heartbeat_at=NOW, version=OLD)]) == placement.Wait("no live hive takes expert")
    assert chosen([headless(heartbeat_at=NOW, excluded=["ci"])], "ci") == placement.Wait("no live hive takes ci")
    current = [headless(heartbeat_at=NOW, version=OLD), headless("current", heartbeat_at=NOW, version=FLOOR)]
    assert placement.place("expert", "claude", "acct", "subscription", REPO, current, NOW) == "current"


@pytest.mark.feature("hive-role-release-floor")
def test_a_preferred_hive_passed_over_for_its_release_is_named_with_both_releases() -> None:
    preferred = headless("preferred", heartbeat_at=NOW, version=OLD, prefer={"expert": 1})
    current = headless("current", heartbeat_at=NOW, version=FLOOR)
    assert chosen([preferred, current]) == placement.Choice("current", spill=placement.Spill("preferred", PASSED_OVER))
    excluded = headless("preferred", heartbeat_at=NOW, version=OLD, prefer={"expert": 1}, excluded=["expert"])
    assert chosen([excluded, current]) == placement.Choice(
        "current", spill=placement.Spill("preferred", "it excludes expert")
    )


@pytest.mark.feature("hive-role-release-floor")
@pytest.mark.parametrize(("version", "placed"), [(OLD, False), (FLOOR, True), ("", True), (DEVELOPMENT, True)])
def test_the_launcher_never_selects_a_hive_older_than_the_first_release_of_the_expert(
    setup: Any, version: str, placed: bool
) -> None:
    setup.herdr._profile_decision.return_value = ProfileDecision("expert", "task", "expert")
    write_hive(setup, ui="no", excluded=["master"], version=version)
    selected = setup.launcher.select(setup.config, "expert", {"id": "expert-1", "requires_harness": "claude"})
    assert (getattr(selected, "hive", None) == setup.hive["id"]) is placed
    assert isinstance(selected, placement.Wait) is not placed


@pytest.mark.feature("hive-role-release-floor")
def test_every_first_release_names_a_role_the_swarm_launches_and_a_release() -> None:
    known = (
        set(naming.TYPES) | set(naming.TYPES.values()) | set(launch.ROLE_PROFILES) | set(launch.ROLE_PROFILES.values())
    )
    assert placement.FIRST_RELEASE and set(placement.FIRST_RELEASE) <= known
    assert all(placement.release(text) is not None for text in placement.FIRST_RELEASE.values())
