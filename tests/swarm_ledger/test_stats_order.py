import json

import pytest

from scripts.swarm_ledger import ledger_layout as layout
from tests.swarm_ledger.test_swarm_layout import browser, open_page

__all__ = ["browser", "open_page"]


@pytest.fixture(autouse=True)
def fresh():
    layout.path().unlink(missing_ok=True)
    yield
    layout.path().unlink(missing_ok=True)


def test_the_stats_order_round_trips_beside_panel_sizes_and_resets_with_them():
    saved = layout.write({"health-box": {"height": 200}, "stats": ["tasks", "phases", "top-bottleneck"]})
    assert saved["stats"] == ["tasks", "phases", "top-bottleneck"]
    assert layout.read() == saved
    assert layout.write({}) == {}


@pytest.mark.parametrize(
    "stats",
    [["Tasks"], ["tasks", "tasks"], ["tasks", 3], "tasks", [f"s{n}" for n in range(layout.MAX_STATS + 1)]],
)
def test_a_bad_stats_order_is_refused(stats):
    with pytest.raises(ValueError):
        layout.write({"stats": stats})


def keys(page):
    return page.tab.eval_on_selector_all("#stats > .stat", "rows => rows.map(r => r.dataset.stat)")


def test_a_stat_row_moves_by_its_grip_and_the_order_is_saved_to_the_shared_layout(open_page):
    page = open_page(width=1920)
    saved = []

    def capture(route):
        if route.request.method == "PUT":
            saved.append(json.loads(route.request.post_data))
        route.fulfill(json=saved[-1] if saved else {})

    page.context.route("**/api/layout", capture)
    before = keys(page)
    assert before[:2] == ["started", "elapsed"] and before[-1] == "top-bottleneck"
    assert page.tab.locator("#stats .stat dt").first.inner_text() == "STARTED"
    page.tab.locator('#stats [data-stat="elapsed"] .stat-grip').focus()
    page.keyboard.press("ArrowUp")
    moved = ["elapsed", "started", *before[2:]]
    assert keys(page) == moved
    page.tab.wait_for_function("() => document.activeElement.matches('[data-stat=elapsed] .stat-grip')")
    page.tab.wait_for_timeout(100)
    assert saved and saved[-1]["stats"] == moved
    page.tab.evaluate("document.dispatchEvent(new Event('stats'))")
    assert keys(page) == moved
