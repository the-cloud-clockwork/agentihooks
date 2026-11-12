"""Browser tests: a layout save still pending when the ledger page closes reaches the server."""

from collections.abc import Callable

import pytest

from tests.swarm_ledger.test_swarm_resize import Tab, base, browser, saved, visit

__all__ = ["base", "browser", "visit"]

GRIP = "[data-grip=height][data-row=handoff-box]"
ARROW_DOWN = f"""() => {{
  const grip = document.querySelector("{GRIP}");
  grip.focus();
  grip.dispatchEvent(new KeyboardEvent("keydown", {{ key: "ArrowDown", bubbles: true }}));
}}"""
RECORD_LAYOUT_WRITES = """() => {
  window.layoutWrites = [];
  const real = window.fetch;
  window.fetch = (url, init) => {
    if (String(url).includes("/api/v1/layout") && init && init.method === "PUT") window.layoutWrites.push(init.keepalive === true);
    return real(url, init);
  };
}"""


@pytest.mark.feature("ledger-layout-unload-save")
def test_a_resize_followed_at_once_by_leaving_the_page_still_saves_the_new_layout(visit: Callable[..., Tab]) -> None:
    page = visit()
    before = page.box("handoff-box")["height"]
    page.tab.evaluate(f"() => {{ ({ARROW_DOWN})(); location.assign('about:blank'); }}")
    page.tab.wait_for_url("about:blank")
    assert saved(("handoff-box",)).get("handoff-box", {}).get("height") == pytest.approx(before + 16, abs=1)


@pytest.mark.feature("ledger-layout-unload-save")
def test_page_hide_sends_a_pending_layout_save_at_once_as_a_keepalive_request(visit: Callable[..., Tab]) -> None:
    page = visit()
    page.tab.evaluate(RECORD_LAYOUT_WRITES)
    page.tab.evaluate(f"() => ({ARROW_DOWN})()")
    assert page.tab.evaluate(
        "() => (window.dispatchEvent(new PageTransitionEvent('pagehide')), window.layoutWrites)"
    ) == [True]
    page.tab.wait_for_timeout(500)
    assert page.tab.evaluate("() => window.layoutWrites") == [True]


@pytest.mark.feature("ledger-layout-unload-save")
def test_a_hidden_page_sends_a_pending_layout_save_at_once_as_a_keepalive_request(visit: Callable[..., Tab]) -> None:
    page = visit()
    page.tab.evaluate(RECORD_LAYOUT_WRITES)
    page.tab.evaluate(f"() => ({ARROW_DOWN})()")
    assert page.tab.evaluate(
        """() => {
          Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
          document.dispatchEvent(new Event("visibilitychange"));
          return window.layoutWrites;
        }"""
    ) == [True]
    page.tab.wait_for_timeout(500)
    assert page.tab.evaluate("() => window.layoutWrites") == [True]
