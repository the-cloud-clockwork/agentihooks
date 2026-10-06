import pytest

from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]


def test_page_shows_the_current_share(tab):
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#cap-codex").input_value() == "30"


def test_codex_and_eng_steps_wait_for_one_apply(tab):
    sent = []

    def receive(route):
        if route.request.method == "PUT":
            sent.append(route.request.post_data_json)
        route.fulfill(json=SWARM)

    tab.route("**/api/swarm/**", receive)
    tab.get_by_role("tab", name="Swarm").click()
    tab.locator('[data-swarm="codex_up"]').click()
    tab.locator('[data-swarm="eng_up"]').click()
    assert sent == []
    tab.locator('[data-swarm="apply"]').click()
    tab.wait_for_function("document.querySelector('#swarm-note').textContent.includes('Apply capacity: done')")
    assert sent == [{"action": "set", "max_eng": 4, "codex_share": 35}]


@pytest.mark.parametrize(
    ("share", "action", "disabled", "expected"),
    [
        (0, "codex_up", "codex_down", 5),
        (100, "codex_down", "codex_up", 95),
        (98, "codex_up", "codex_down", 100),
        (2, "codex_down", "codex_up", 0),
    ],
)
def test_codex_step_bounds(tab, share, action, disabled, expected):
    sent = []
    sw = {**SWARM, "config": {**SWARM["config"], "codex_share": share}}

    def receive(route):
        if route.request.method == "PUT":
            sent.append(route.request.post_data_json)
        route.fulfill(json=sw)

    tab.route("**/api/swarm/**", receive)
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    if share in (0, 100):
        assert tab.locator(f'[data-swarm="{disabled}"]').is_disabled()
    tab.locator(f'[data-swarm="{action}"]').click()
    assert tab.locator("#cap-codex").input_value() == str(expected)
    tab.locator('[data-swarm="apply"]').click()
    tab.wait_for_function("document.querySelector('#swarm-note').textContent.includes('done')")
    assert sent == [{"action": "set", "codex_share": expected}]
