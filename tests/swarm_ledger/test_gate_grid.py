import pytest

from scripts.gates.catalog import defaults
from tests.swarm_ledger.test_swarm_layout import browser, open_page, status

__all__ = ["browser", "open_page"]

ORDER = [
    "identity",
    "watch",
    "subagents",
    "reruns",
    "intent",
    "build",
    "claim-stop",
    "push-stop",
    "quiet",
    "one-push",
    "claims",
    "trace-plan",
    "talk",
]


def test_gates_sit_five_per_row_with_choices_in_fixed_columns_at_1920(open_page):
    page = open_page(status(gate_modes=defaults()), width=1920)
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    rows = page.tab.eval_on_selector_all(
        ".sw-gate",
        """rows => rows.map(row => {
          const box = row.getBoundingClientRect();
          const name = row.querySelector('.sw-cap-name');
          const line = parseFloat(getComputedStyle(name).lineHeight);
          return {
            name: name.textContent, x: box.x, y: box.y, width: box.width,
            align: getComputedStyle(name).textAlign,
            lines: Math.round(name.getBoundingClientRect().height / line),
            clipped: name.scrollWidth > name.clientWidth,
            choices: [...row.querySelectorAll('button')].map(button => {
              const at = button.getBoundingClientRect();
              return [at.x - box.x, at.y - box.y, at.width];
            }),
          };
        })""",
    )
    assert [row["name"] for row in rows] == ORDER
    assert len({row["x"] for row in rows}) == 5
    for row in rows:
        assert row["align"] == "right"
        assert row["lines"] == 1 and not row["clipped"], row["name"]
        assert row["width"] == pytest.approx(rows[0]["width"])
        assert row["choices"] == rows[0]["choices"]
    for index, row in enumerate(rows):
        assert row["x"] == rows[index % 5]["x"]
        assert row["y"] == rows[index - index % 5]["y"]
    assert rows[5]["y"] > rows[0]["y"] and rows[10]["y"] > rows[5]["y"]
    caps = page.tab.locator(".sw-caps").bounding_box()
    label = page.tab.locator("#capacity-box > .sw-label").first.bounding_box()
    gates = page.tab.locator("#gates-label").bounding_box()
    assert caps["y"] <= label["y"] + label["height"] / 2 <= caps["y"] + caps["height"]
    assert gates["y"] >= caps["y"] + caps["height"]
    grid = page.tab.locator("#swarm-gates").bounding_box()
    assert grid["x"] + grid["width"] <= page.box("capacity-box")["x"] + page.box("capacity-box")["width"]


def test_every_gate_name_has_a_short_shared_tooltip(open_page):
    page = open_page(status(gate_modes=defaults()))
    tips = page.tab.eval_on_selector_all(
        ".sw-gate .sw-cap-name", "names => names.map(name => [name.textContent, window.ledgerTip(name)])"
    )
    assert {name for name, _ in tips} == set(defaults())
    for name, tip in tips:
        assert tip, name
        assert len(tip.split()) <= 25, name
    name = page.tab.locator(".sw-gate .sw-cap-name").first
    name.hover()
    page.tab.locator(".ledger-tip:popover-open").wait_for(timeout=3000)
    assert page.tab.locator(".ledger-tip").inner_text() == tips[0][1]


def test_labels_are_bright_and_flat_while_only_the_selected_mode_takes_its_colour(open_page):
    page = open_page(status(gate_modes=defaults()))
    styles = page.tab.eval_on_selector_all(
        ".sw-cap-name",
        "names => names.map(name => [getComputedStyle(name).color, getComputedStyle(name).textShadow])",
    )
    text = page.tab.locator("#capacity-box > .sw-label").first.evaluate("label => getComputedStyle(label).color")
    assert all(color == text and shadow == "none" for color, shadow in styles)
    modes = page.tab.eval_on_selector_all(
        ".sw-gate button",
        "buttons => buttons.map(button => [button.getAttribute('aria-pressed'), getComputedStyle(button).color, getComputedStyle(button).textShadow])",
    )
    selected = next(color for pressed, color, _ in modes if pressed == "true")
    assert all(color == selected and shadow == "none" for pressed, color, shadow in modes if pressed == "true")
    assert all(color != selected and shadow == "none" for pressed, color, shadow in modes if pressed == "false")
