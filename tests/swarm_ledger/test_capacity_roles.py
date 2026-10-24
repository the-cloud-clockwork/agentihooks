from tests.swarm_ledger.ledger_page import fulfill_events, is_events
from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]

ROLES = {
    "eng": ["engineer", "frontend", "qa", "documentation"],
    "ci": ["cicd"],
    "plan": ["planner"],
    "seats": ["dispatcher", "master"],
}


def roles(tab):
    return tab.evaluate(
        """() => [...document.querySelectorAll("#capacity-roles [data-lane]")].map((lane) => [lane.dataset.lane,
          [...lane.querySelectorAll("[data-role]")].map((row) => [row.dataset.role, row.querySelector("[data-role-count]").textContent])])"""
    )


def test_a_status_with_agents_of_every_role_renders_one_row_per_role_with_its_live_count(tab):
    agents = [
        {"name": "m", "lane": "master"},
        {"name": "d", "lane": "dispatch"},
        {"name": "e1", "lane": "eng"},
        {"name": "e2", "lane": "eng", "profile": "engineer"},
        {"name": "e3", "lane": "eng", "profile": "engineer", "state": "finished"},
        {"name": "f1", "lane": "eng", "profile": "frontend"},
        {"name": "q1", "lane": "eng", "profile": "qa"},
        {"name": "q2", "lane": "eng", "profile": "qa"},
        {"name": "q3", "lane": "eng", "profile": "qa"},
        {"name": "doc", "lane": "eng", "profile": "documentation"},
        {"name": "c1", "lane": "ci"},
        {"name": "p1", "lane": "plan", "profile": "planner"},
    ]
    status = {**SWARM, "config": {**SWARM["config"], "max_plan": 1}, "agents": agents}
    tab.route("**/api/v1/**", lambda route: fulfill_events(route, swarm=status))
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    tab.locator("#capacity-roles [data-role]").first.wait_for(state="visible", timeout=1500)
    counts = {"engineer": "2", "frontend": "1", "qa": "3", "documentation": "1", "cicd": "1", "planner": "1"}
    counts |= {"dispatcher": "1", "master": "1"}
    assert roles(tab) == [[lane, [[role, counts[role]] for role in names]] for lane, names in ROLES.items()]
    assert tab.locator("#cap-eng").input_value() == "3"
    assert tab.locator("#capacity-unavailable").is_hidden()


def test_an_unreadable_status_renders_the_unavailable_notice_and_no_empty_inputs(tab):
    tab.route("**/api/v1/**", lambda route: fulfill_events(route, swarm=None))
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    notice = tab.locator("#capacity-unavailable")
    notice.wait_for(state="visible", timeout=1500)
    assert "swarm status" in notice.inner_text().lower()
    visible = tab.evaluate(
        """() => [...document.querySelectorAll("#capacity-box input, #capacity-box select, #capacity-box button, #capacity-box [data-role]")]
          .filter((el) => el.checkVisibility()).map((el) => el.id || el.dataset.role || el.dataset.swarm)"""
    )
    assert visible == []


def test_a_failed_read_after_a_status_keeps_the_last_counts_under_the_unavailable_notice(tab):
    reads = []

    def events(route):
        if not is_events(route.request.url):
            return route.fallback()
        reads.append(route.request.url)
        if len(reads) == 1:
            fulfill_events(route, swarm=SWARM)
        else:
            route.abort()

    tab.route("**/api/v1/**", events)
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    notice = tab.locator("#capacity-unavailable")
    notice.wait_for(state="visible", timeout=5000)
    assert "last observed" in notice.inner_text().lower()
    assert tab.locator("#cap-eng").input_value() == "3"
    count = '#capacity-roles [data-role="{}"] [data-role-count]'
    assert tab.locator(count.format("engineer")).inner_text() == "1"
    assert tab.locator(count.format("documentation")).inner_text() == "0"
