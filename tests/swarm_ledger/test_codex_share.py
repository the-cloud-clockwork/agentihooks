import pytest

from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]


def test_page_shows_current_share_and_live_split(tab):
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#cap-codex").input_value() == "30"
    assert "So far 6 of 19 started on Codex (31%)." in tab.locator("#codex-split").inner_text()



def test_codex_steps_are_five_and_apply_sends_the_draft(tab):
    sent = []

    def receive(route):
        if route.request.method == "PUT":
            sent.append(route.request.post_data_json)
        route.fulfill(json=SWARM)

    tab.route("**/api/swarm/**", receive)
    tab.get_by_role("tab", name="Swarm").click()
    tab.locator('[data-swarm="codex_up"]').click()
    tab.locator('[data-swarm="eng_up"]').click()
    assert tab.locator("#cap-codex").input_value() == "35"
    assert tab.locator("#cap-eng").input_value() == "4"
    tab.locator('[data-swarm="set"]').click()
    tab.wait_for_function("document.querySelector('#swarm-note').textContent.includes('done')")
    assert sent[-1] == {"action": "set", "max_eng": 4, "max_ci": 1, "codex_share": 35}


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
    sw = {**SWARM, "config": {**SWARM["config"], "codex_share": share}}
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json=sw))
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    if share in (0, 100):
        assert tab.locator(f'[data-swarm="{disabled}"]').is_disabled()
    tab.locator(f'[data-swarm="{action}"]').click()
    assert tab.locator("#cap-codex").input_value() == str(expected)


def test_no_spawns_shows_zero_actual_percent(tab):
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json={**SWARM, "spawns": {}}))
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    assert "So far 0 of 0 started on Codex (0%)." in tab.locator("#codex-split").inner_text()
