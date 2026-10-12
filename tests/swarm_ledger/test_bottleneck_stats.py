import pytest

from tests.swarm_ledger.test_swarm_layout import browser, open_page, status

__all__ = ["browser", "open_page"]


def test_top_stage_trend_and_action_fit_the_operator_viewport(open_page):
    page = open_page(
        status(
            top_bottleneck={
                "stage": "ci_overrun",
                "label": "ci overrun",
                "trend": "rising",
                "last_action": "Proposed a faster mutation run",
                "seconds": 10800.0,
                "measured": "5 minutes ago",
            }
        ),
        width=1920,
    )
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    box = page.tab.locator("#stats .top-bottleneck")
    assert page.tab.locator("#stats > .stat").last.evaluate("e => e.classList.contains('top-bottleneck')")
    assert box.locator("li").count() == 4
    box.scroll_into_view_if_needed()
    assert box.is_visible()
    assert box.inner_text() == (
        "TOP BOTTLENECK\nci overrun\nTrend rising\nMeasured 5 minutes ago\nLast action Proposed a faster mutation run"
    )
    bounds = box.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 1920


@pytest.mark.parametrize(
    "top,shown",
    [
        (None, "TOP BOTTLENECK\nunavailable\nTrend unknown\nLast action none recorded"),
        ({"stage": ""}, "TOP BOTTLENECK\nnone\nTrend unknown\nMeasured never\nLast action none recorded"),
    ],
)
def test_stats_distinguish_missing_status_from_no_active_finding(open_page, top, shown):
    page = open_page(status(top_bottleneck=top), width=1920)
    assert page.tab.locator("#stats .top-bottleneck").inner_text() == shown


def test_action_is_text_and_long_actions_wrap_within_stats(open_page):
    action = '<img src=x onerror="window.injected=true"> ' + "Proposed a remedy " * 8
    page = open_page(
        status(top_bottleneck={"stage": "build", "label": "build", "trend": "steady", "last_action": action}),
        width=1920,
    )
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    box = page.tab.locator("#stats .top-bottleneck")
    assert action.strip() in box.inner_text()
    assert box.locator("img").count() == 0
    assert box.evaluate("e => e.scrollWidth <= e.clientWidth")
