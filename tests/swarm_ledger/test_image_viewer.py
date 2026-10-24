import base64
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, shell_html, show
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser
from tests.swarm_ledger.test_media import png

PALETTE = Path(__file__).resolve().parents[2] / "scripts/swarm_ledger/palette.css"
browser = chromium_browser


@pytest.fixture
def image_page(browser, request):
    tab = browser.new_page(viewport={"width": 1440, "height": 900})
    images = [
        {"id": "a" * 64 + ".png", "width": 1280, "height": 720},
        {"id": "b" * 64 + ".png", "width": 120, "height": 80},
    ]
    entry = {"id": "slide", "by": "image-engineer", "at": 1, "text": "Reference slides", "attachments": images}
    doc = {"title": "Image proof", "phases": [{"id": "p1", "title": "Slides", "comments": [entry]}], "chat": [entry]}
    tab.route("**/media/**", lambda route: route.fulfill(body=png(1280, 720), content_type="image/png"))
    html = shell_html()
    html = html.replace("__LEDGER_PALETTE__", PALETTE.read_text())
    show(tab, html, ledger=ledger_state(doc))
    tab.locator("#item-phases-p1 details[data-key]").evaluate("el => el.open = true")
    for image in tab.locator(".attach-row img").all():
        source = (
            "data:image/png;base64,"
            + base64.b64encode(png(int(image.get_attribute("width")), int(image.get_attribute("height")))).decode()
        )
        image.evaluate(
            """(el, {source, delay}) => {
              el.removeAttribute("src");
              el.removeAttribute("width");
              el.removeAttribute("height");
              setTimeout(() => { el.src = source; }, delay);
            }""",
            {"source": source, "delay": getattr(request, "param", 0)},
        )
    yield tab
    tab.close()


@pytest.mark.parametrize("image_page", [0, 250], indirect=True)
@pytest.mark.parametrize("width", [1440, 390])
def test_comment_images_are_small_thumbnails_that_keep_ratio_and_never_grow(image_page, width):
    tab = image_page
    tab.set_viewport_size({"width": width, "height": 900})
    tab.wait_for_function(
        """() => {
          const images = [...document.querySelectorAll("#item-phases-p1 .attach-row img")];
          return images.length === 2 && images.every(image =>
            image.complete && image.naturalWidth > 0 && image.naturalHeight > 0 &&
            image.getBoundingClientRect().width > 0);
        }"""
    )
    large = tab.locator("#item-phases-p1 .attach-row img").nth(0).bounding_box()
    small = tab.locator("#item-phases-p1 .attach-row img").nth(1).bounding_box()
    assert large["width"] == 160
    assert abs(large["width"] / large["height"] - 1280 / 720) < 0.01
    assert small["width"] == 120
    assert small["height"] == 80


def test_viewer_keeps_page_full_size_navigation_and_three_close_paths(image_page):
    tab = image_page
    first = tab.locator("#item-phases-p1 .thumb").first
    original_url = tab.url
    first.click()
    viewer = tab.get_by_role("dialog", name="Image viewer")
    assert viewer.is_visible()
    assert len(tab.context.pages) == 1 and tab.url == original_url
    assert viewer.locator("img").get_attribute("width") == "1280"
    assert viewer.locator("img").bounding_box()["width"] == 1280
    tab.get_by_role("button", name="Next image").click()
    assert viewer.locator("img").get_attribute("width") == "120"
    tab.get_by_role("button", name="Previous image").click()
    assert viewer.locator("img").get_attribute("width") == "1280"
    tab.keyboard.press("Escape")
    assert not viewer.is_visible()
    assert first.evaluate("el => el === document.activeElement")
    first.click()
    tab.get_by_role("button", name="Close viewer").click()
    assert not viewer.is_visible()
    first.click()
    viewer.click(position={"x": 3, "y": 3})
    assert not viewer.is_visible()


def test_phone_viewer_preserves_full_size_with_scroll_and_chat_opens_viewer(image_page):
    tab = image_page
    tab.set_viewport_size({"width": 390, "height": 844})
    tab.locator("#chat-fab").click()
    tab.locator("#chat-panel .thumb").first.click()
    viewer = tab.get_by_role("dialog", name="Image viewer")
    assert viewer.is_visible()
    assert viewer.locator("img").bounding_box()["width"] == 1280
    scroll = viewer.locator(".image-scroll")
    assert scroll.evaluate("el => el.scrollWidth > el.clientWidth")
    tab.keyboard.press("Escape")
    assert not viewer.is_visible()
