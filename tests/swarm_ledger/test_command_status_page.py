from tests.swarm_ledger.test_swarm_layout import browser, open_page, status

__all__ = ["browser", "open_page"]


def test_failed_http_control_keeps_its_error_over_old_acknowledgement(open_page):
    old = [{"id": "old", "command": "swarm", "argv": ["pause"], "state": "acknowledged", "error": ""}]
    page = open_page(status(commands=old), width=1920)
    page.tab.route("**/api/swarm/*", lambda route: route.fulfill(status=502, body="queue unavailable"))
    page.tab.locator('[data-swarm="stop"]').click()
    page.tab.wait_for_function(
        "document.querySelector('#swarm-note').textContent.includes('queue unavailable')", timeout=3000
    )
    assert "Could not stop" in page.text("#swarm-note")
