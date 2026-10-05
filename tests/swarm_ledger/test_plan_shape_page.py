import pytest

pytestmark = pytest.mark.unit


from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]


def test_page_displays_only_labelled_counts_from_status(tab):
    sw = {**SWARM, "plan_shape": {"has_dependencies": True, "chain_length": 2, "parallel_width": 3}}
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json=sw))
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#swarm-plan-shape").inner_text() == "2\nChain\n3\nParallel"


def test_page_hides_missing_report_and_independent_work(tab):
    sw = {**SWARM, "plan_shape": {"has_dependencies": False, "chain_length": 1, "parallel_width": 3}}
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json=sw))
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#swarm-plan-shape").is_hidden()
