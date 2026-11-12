"""Browser proof that the swarm status line keeps each failure until it is dismissed or its control succeeds."""

from collections.abc import Callable

from tests.swarm_ledger.test_swarm_layout import Page, browser, open_page, status

__all__ = ["browser", "open_page"]

OpenPage = Callable[..., Page]
QUOTA_FAILED = {"id": "a", "command": "quota", "argv": [], "state": "failed", "error": "quota reader timed out"}
QUOTA_TEXT = "Could not refresh quota: quota reader timed out. Try again or ask the master."


def fail_controls(page: Page, reason: str) -> None:
    """Answer every swarm control write with a server error naming the reason."""
    page.tab.route("**/swarm/controls", lambda route: route.fulfill(status=502, body=reason))


def note(page: Page) -> str:
    """Return the status line's text content."""
    return page.tab.locator("#swarm-note").text_content() or ""


def wait_note(page: Page, expression: str) -> None:
    """Wait until the status line text, bound as text, satisfies the expression."""
    page.tab.wait_for_function(
        f"() => (text => {expression})(document.querySelector('#swarm-note').textContent)", timeout=3000
    )


def click_and_settle(page: Page, action: str) -> None:
    """Click a swarm control and wait until its request has settled."""
    page.tab.locator(f'[data-swarm="{action}"]').click()
    page.tab.wait_for_function(
        f"() => !document.querySelector('[data-swarm=\"{action}\"]').classList.contains('sending')", timeout=3000
    )


def test_a_failed_command_row_survives_a_later_accepted_command(open_page: OpenPage) -> None:
    rows = [QUOTA_FAILED, {"id": "b", "command": "swarm", "argv": ["pause"], "state": "accepted", "error": ""}]
    page = open_page(status(commands=rows), width=1920)
    assert note(page) == QUOTA_TEXT


def test_a_failed_control_survives_another_accepted_control_and_clears_on_dismiss(open_page: OpenPage) -> None:
    page = open_page(status(), width=1920)
    fail_controls(page, "queue unavailable")
    page.tab.locator('[data-swarm="stop"]').click()
    wait_note(page, "text.includes('queue unavailable')")
    page.tab.unroute("**/swarm/controls")
    click_and_settle(page, "pause")
    assert page.puts == [{"action": "pause"}]
    assert note(page) == "Could not stop the swarm: queue unavailable. Try again or ask the master."
    page.tab.get_by_role("button", name="Dismiss: Could not stop the swarm").click()
    assert note(page) == ""
    assert not page.tab.locator("#swarm-note").is_visible()


def test_a_failed_control_clears_when_the_same_control_succeeds(open_page: OpenPage) -> None:
    page = open_page(status(), width=1920)
    fail_controls(page, "queue unavailable")
    page.tab.locator('[data-swarm="stop"]').click()
    wait_note(page, "text.includes('queue unavailable')")
    page.tab.unroute("**/swarm/controls")
    click_and_settle(page, "stop")
    wait_note(page, "text === ''")
    assert page.puts == [{"action": "stop"}]


def test_a_failed_command_row_clears_when_a_later_row_of_the_same_control_is_accepted(open_page: OpenPage) -> None:
    rows = [
        QUOTA_FAILED,
        {"id": "b", "command": "swarm", "argv": ["pause"], "state": "failed", "error": "lease is stale"},
        {"id": "c", "command": "quota", "argv": [], "state": "acknowledged", "error": ""},
    ]
    page = open_page(status(commands=rows), width=1920)
    assert note(page) == "Could not pause the swarm: lease is stale. Try again or ask the master."


def test_two_failures_show_together_until_each_is_dismissed(open_page: OpenPage) -> None:
    rows = [
        QUOTA_FAILED,
        {"id": "b", "command": "swarm", "argv": ["pause"], "state": "failed", "error": "lease is stale"},
    ]
    page = open_page(status(commands=rows), width=1920)
    assert page.tab.locator("#swarm-note [data-dismiss]").count() == 2
    page.tab.get_by_role("button", name="Dismiss: Could not pause the swarm").click()
    assert note(page) == QUOTA_TEXT


def test_a_dismissed_command_row_stays_dismissed_when_the_line_redraws(open_page: OpenPage) -> None:
    page = open_page(status(commands=[QUOTA_FAILED]), width=1920)
    page.tab.get_by_role("button", name="Dismiss: Could not refresh quota").click()
    click_and_settle(page, "pause")
    assert page.puts == [{"action": "pause"}]
    assert note(page) == ""
