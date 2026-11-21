import pytest

from scripts.events import scope

pytestmark = pytest.mark.feature("swarm-review-lane-reporting")


@pytest.mark.parametrize("name", ["review", "swarm.rig.ready.review"])
def test_review_queue_is_owned_by_its_swarm(name: str) -> None:
    assert scope.queue_lane("rig", name) == "review"


@pytest.mark.parametrize("name", ["swarm.other.ready.review", "reviewer"])
def test_review_queue_preserves_owner_and_lane_validation(name: str) -> None:
    with pytest.raises(ValueError):
        scope.queue_lane("rig", name)
