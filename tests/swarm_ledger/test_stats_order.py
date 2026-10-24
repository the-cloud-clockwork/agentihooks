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
    saved = layout.write({"handoff-box": {"height": 200}, "stats": ["tasks", "phases", "top-bottleneck"]})
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


def test_the_stats_order_holds_up_to_its_limit_beside_other_rows():
    order = [f"s{n}" for n in range(layout.MAX_STATS)]
    saved = layout.write({"stats": order, "handoff-box": {"height": 200}})
    assert saved == {"stats": order, "handoff-box": {"height": 200}}


def test_each_stats_refusal_names_its_cause():
    with pytest.raises(ValueError, match=f"^stats must list at most {layout.MAX_STATS} rows$"):
        layout.write({"stats": "tasks"})
    with pytest.raises(ValueError, match="^stats must list distinct row keys$"):
        layout.write({"stats": ["tasks", "tasks"]})


def keys(page):
    return page.tab.eval_on_selector_all("#stats > .stat", "rows => rows.map(r => r.dataset.stat)")


def test_a_stat_row_moves_by_its_grip_and_the_order_is_saved_to_the_shared_layout(open_page):
    page = open_page(width=1920)
    saved = []

    def capture(route):
        if route.request.method == "PUT":
            saved.append(json.loads(route.request.post_data))
        route.fulfill(json={"data": saved[-1] if saved else {}})

    page.context.route("**/api/v1/layout", capture)
    before = keys(page)
    assert before[:2] == ["started", "elapsed"] and before[-1] == "top-bottleneck"
    assert page.tab.locator("#stats .stat dt").first.inner_text() == "STARTED"
    page.tab.locator('#stats [data-stat="elapsed"] .stat-grip').focus()
    page.tab.keyboard.press("ArrowUp")
    moved = ["elapsed", "started", *before[2:]]
    assert keys(page) == moved
    page.tab.wait_for_function("() => document.activeElement.matches('[data-stat=elapsed] .stat-grip')")
    page.tab.wait_for_timeout(100)
    assert saved and saved[-1]["stats"] == moved
    page.tab.evaluate("document.dispatchEvent(new Event('stats'))")
    assert keys(page) == moved


def drag(page, key, to_key, fraction):
    grip = page.tab.locator(f'#stats [data-stat="{key}"] .stat-grip').bounding_box()
    target = page.tab.locator(f'#stats [data-stat="{to_key}"]').bounding_box()
    x, y = grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2
    page.tab.mouse.move(x, y)
    page.tab.mouse.down()
    for step in range(1, 13):
        page.tab.mouse.move(x + step, y + (target["y"] + target["height"] * fraction - y) * step / 12)
    page.tab.mouse.up()


def test_a_mouse_drag_moves_a_stat_row_down_and_back_up(open_page):
    page = open_page(width=1920)
    page.context.route("**/api/v1/layout", lambda route: route.fulfill(json={"data": {}}))
    before = keys(page)
    drag(page, "elapsed", before[4], 0.75)
    assert keys(page) == [before[0], *before[2:5], "elapsed", *before[5:]]
    drag(page, "elapsed", "started", 0.25)
    assert keys(page) == ["elapsed", "started", *before[2:]]


def test_the_grip_keeps_the_same_margin_on_the_left_as_the_values_on_the_right(open_page):
    page = open_page(width=1920)
    margins = page.tab.evaluate(
        """() => {
            const panel = document.querySelector('#stats-column section').getBoundingClientRect();
            const grip = document.querySelector('#stats .stat-grip').getBoundingClientRect();
            const value = document.querySelector('#stats .stat dd').getBoundingClientRect();
            return [Math.round(grip.left - panel.left), Math.round(panel.right - value.right)];
        }"""
    )
    assert margins[0] == margins[1] == 16
