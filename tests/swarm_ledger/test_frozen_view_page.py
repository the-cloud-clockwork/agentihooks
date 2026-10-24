import pytest

from tests.swarm_ledger.test_freeze_page import browser, freeze, page

__all__ = ["browser", "page"]


def present(tab, *ids):
    return tab.evaluate("(ids) => ids.map((id) => !!document.getElementById(id))", list(ids))


def hide_frozen(tab):
    tab.click("#frozen-view")
    tab.wait_for_function("() => document.querySelector('#frozen-view').getAttribute('aria-pressed') === 'true'")


def toggle_state(tab):
    return tab.evaluate(
        "() => { const b = document.querySelector('#frozen-view'); return [b.getAttribute('aria-pressed'), b.textContent]; }"
    )


def test_the_toggle_starts_off_and_shows_every_row(page):
    tab, _ = page
    assert toggle_state(tab) == ["false", "Hide frozen"]
    assert present(tab, "item-tasks-t1", "item-tasks-t2", "item-tasks-t3") == [True, True, True]


@pytest.mark.parametrize("page", [[freeze("plans/v2")]], indirect=True)
def test_hiding_frozen_drops_frozen_and_held_rows_and_counts_them(page):
    tab, _ = page
    hide_frozen(tab)
    assert toggle_state(tab) == ["true", "Show all"]
    tab.wait_for_function("() => !document.getElementById('item-tasks-t2')")
    assert present(tab, "item-tasks-t1", "item-tasks-t2", "item-tasks-t3", "item-tasks-t5") == [
        True,
        False,
        True,
        False,
    ]
    assert present(tab, "item-plans-hier", "item-plans-v2") == [True, False]
    assert tab.locator("#tasks-count").text_content().endswith(" · 2 visible · 2 hidden")


@pytest.mark.parametrize("page", [[freeze("phases/p1"), freeze("plans/v2")]], indirect=True)
def test_a_plan_whose_rows_are_all_hidden_hides_with_them(page):
    tab, _ = page
    hide_frozen(tab)
    tab.wait_for_function("() => !document.getElementById('item-tasks-t1')")
    assert present(tab, "item-plans-hier", "item-phases-p1", "item-tasks-t1", "item-tasks-t3") == [
        False,
        False,
        False,
        False,
    ]
    assert tab.locator("#tasks .empty").text_content() == "All items are hidden."


@pytest.mark.parametrize("page", [[freeze("plans/v2")]], indirect=True)
def test_show_all_brings_the_frozen_rows_back(page):
    tab, _ = page
    hide_frozen(tab)
    tab.click("#frozen-view")
    tab.wait_for_function("() => !!document.getElementById('item-tasks-t2')")
    assert toggle_state(tab) == ["false", "Hide frozen"]
    assert "hidden" not in tab.locator("#tasks-count").text_content()


@pytest.mark.parametrize("page", [[freeze("plans/v2")]], indirect=True)
def test_the_choice_survives_a_reload(page):
    tab, _ = page
    hide_frozen(tab)
    slug = tab.evaluate("() => document.querySelector('meta[name=ledger-slug]').content")
    assert tab.evaluate("(k) => localStorage.getItem(k)", f"plan-ledger:{slug}:hide-frozen") == "true"
    tab.reload()
    tab.wait_for_function("() => document.querySelector('#frozen-view').getAttribute('aria-pressed') === 'true'")
    assert toggle_state(tab) == ["true", "Show all"]


@pytest.mark.parametrize("page", [[freeze("plans/v2")]], indirect=True)
def test_jumping_to_a_hidden_task_reveals_it(page):
    tab, _ = page
    hide_frozen(tab)
    tab.wait_for_function("() => !document.getElementById('item-tasks-t2')")
    tab.evaluate(
        """() => import(new URL("outline.js", document.querySelector("script[type=module][src$='/js/main.js']").src))
          .then((m) => m.revealTarget("item-tasks-t2"))"""
    )
    tab.wait_for_function("() => !!document.getElementById('item-tasks-t2')")
    assert present(tab, "item-tasks-t1", "item-tasks-t2") == [True, True]
