import pytest

from tests.swarm_ledger.test_swarm_resize import base, browser, saved, visit

__all__ = ["base", "browser", "visit"]

COLUMNS = ("outline", "main-content", "stats-column")


def short_columns(page):
    window = page.tab.evaluate("innerHeight")
    bottoms = {column: page.box(column)["y"] + page.box(column)["height"] for column in COLUMNS}
    return {column: bottom for column, bottom in bottoms.items() if not window - 25 <= bottom <= window}


def views_reach_the_bottom(page):
    for view in ("Ledger", "Swarm"):
        page.tab.get_by_role("tab", name=view).click()
        assert short_columns(page) == {}, view


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720)])
def test_columns_reach_the_window_bottom_in_both_views_after_a_pane_resize_and_a_reload(visit, size):
    page = visit(width=size[0])
    page.tab.set_viewport_size({"width": size[0], "height": size[1]})
    views_reach_the_bottom(page)
    page.drag("height", "health-box", dy=120)
    saved(("health-box",), page)
    views_reach_the_bottom(page)
    page.reload()
    views_reach_the_bottom(page)


@pytest.mark.parametrize("zoom", ["0.67", "1.5"])
def test_columns_reach_the_window_bottom_when_the_page_is_zoomed(visit, zoom):
    page = visit(width=1920)
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    page.tab.evaluate(f"document.documentElement.style.zoom = '{zoom}'")
    views_reach_the_bottom(page)
