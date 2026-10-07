import pytest

from tests.swarm_ledger.test_swarm_layout import browser, open_page, status

__all__ = ["browser", "open_page"]


@pytest.mark.parametrize("state", ["pending", "accepted", "failed", "acknowledged"])
def test_command_log_keeps_each_state_visible(open_page, state):
    commands = [
        {
            "id": "old",
            "command": "swarm",
            "argv": ["pause"],
            "state": state,
            "error": "refused" if state == "failed" else "",
        },
        {"id": "new", "command": "swarm", "argv": ["start"], "state": "acknowledged", "error": ""},
    ]
    page = open_page(status(commands=commands), width=1920)
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    page.tab.locator("#command-log summary").click()
    assert page.tab.locator("#command-log").inner_text().count(state) >= 1
    assert "Pause" in page.tab.locator("#command-log").inner_text()
    if state == "failed":
        assert "refused" in page.tab.locator("#command-log").inner_text()


def test_failed_http_control_keeps_its_error_over_old_acknowledgement(open_page):
    old = [{"id": "old", "command": "swarm", "argv": ["pause"], "state": "acknowledged", "error": ""}]
    page = open_page(status(commands=old), width=1920)
    page.tab.route("**/api/swarm/*", lambda route: route.fulfill(status=502, body="queue unavailable"))
    page.tab.locator('[data-swarm="stop"]').click()
    page.tab.wait_for_function(
        "document.querySelector('#swarm-note').textContent.includes('queue unavailable')", timeout=3000
    )
    assert "Could not stop" in page.text("#swarm-note")
