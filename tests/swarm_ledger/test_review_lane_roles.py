import pytest
from playwright.sync_api import Page

from tests.swarm_ledger.ledger_page import fulfill_events
from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]
pytestmark = pytest.mark.feature("swarm-review-lane-reporting")


@pytest.mark.feature("swarm-review-lane-reporting")
def test_page_lists_the_review_lane_and_counts_only_live_reviewers(tab: Page) -> None:
    agents = [
        {"name": "reviewer@abcdef-0001", "lane": "review", "profile": "reviewer"},
        {"name": "reviewer@abcdef-0002", "lane": "review"},
        {"name": "reviewer@abcdef-0003", "lane": "review", "profile": "reviewer", "state": "finished"},
    ]
    tab.route("**/api/v1/**", lambda route: fulfill_events(route, swarm={**SWARM, "agents": agents}))
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    group = tab.locator('#capacity-roles [data-lane="review"]')
    group.wait_for(state="visible", timeout=1500)
    assert group.get_attribute("aria-label") == "review roles"
    assert group.locator('[data-role="reviewer"] [data-role-count]').inner_text() == "2"


@pytest.mark.feature("swarm-review-lane-reporting")
@pytest.mark.parametrize(
    ("config", "agents", "expected"),
    [
        ({}, [], 0),
        ({"max_review": 0}, [], 1),
        ({}, [{"name": "reviewer@abcdef-0001", "lane": "review"}], 1),
        ({}, [{"name": "reviewer@abcdef-0001", "lane": "review", "state": "finished"}], 0),
    ],
    ids=["legacy-status", "configured-zero", "live-reviewer", "finished-reviewer"],
)
def test_page_keeps_review_roles_compatible_with_published_status(
    tab: Page, config: dict[str, int], agents: list[dict[str, str]], expected: int
) -> None:
    tab.route("**/api/v1/**", lambda route: fulfill_events(route, swarm={**SWARM, "config": config, "agents": agents}))
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    tab.get_by_role("group", name="eng roles", exact=True).wait_for(state="visible", timeout=1500)
    assert tab.locator('#capacity-roles [data-lane="review"]').count() == expected
