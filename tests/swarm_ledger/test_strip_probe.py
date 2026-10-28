import json
import os
import subprocess
import sys

import pytest

from tests.swarm_ledger.test_strip_panels import browser, tab  # noqa: F401

RECORD = """() => {
  window.__log = [];
  const s = document.getElementById("icon-strip");
  const rec = (k) => window.__log.push([k, performance.now() | 0, s.scrollTop, s.scrollHeight, s.clientHeight, innerHeight,
    document.getElementById("bell").getBoundingClientRect().top, document.activeElement && document.activeElement.id]);
  s.addEventListener("scroll", () => rec("scroll"));
  addEventListener("resize", () => rec("resize"));
  new ResizeObserver(() => rec("ro")).observe(s);
  new MutationObserver((m) => rec("mut:" + m.map((x) => x.type + ":" + (x.target.id || x.target.className)).join(","))).observe(s, {childList: true, subtree: true, attributes: true});
  window.__rec = rec;
}"""


@pytest.fixture(scope="module", autouse=True)
def busy():
    procs = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(os.cpu_count() or 2)]
    yield
    for proc in procs:
        proc.kill()


@pytest.mark.parametrize("rep", range(40))
def test_probe(tab, rep):  # noqa: F811
    if tab.viewport_size["width"] < 1024:
        return
    tab.set_default_timeout(20000)
    tab.evaluate(RECORD)
    beside = """() => {
      const panel = document.getElementById("notif").getBoundingClientRect();
      const icon = document.getElementById("bell").getBoundingClientRect();
      return Math.abs(panel.top - icon.top) < 0.5 && panel.left >= document.getElementById("icon-strip").getBoundingClientRect().right;
    }"""
    bell, strip = tab.locator("#bell"), tab.locator("#icon-strip")
    bell.click()
    tab.evaluate("() => __rec('clicked')")
    start = bell.bounding_box()["y"]
    tab.set_viewport_size({"width": 1280, "height": 320})
    tab.evaluate("() => __rec('viewport')")
    moved = bell.bounding_box()["y"]
    tab.wait_for_function(beside)
    tab.evaluate("() => __rec('beside1')")
    assert strip.evaluate("el => el.scrollHeight > el.clientHeight")
    strip.evaluate("el => { el.scrollTop = 40; __rec('set'); }")
    tab.wait_for_function(beside)
    tab.evaluate("() => __rec('beside2')")
    got = bell.bounding_box()["y"]
    tab.evaluate("() => __rec('read')")
    log = tab.evaluate("() => __log")
    if rep == 0 or got != pytest.approx(moved - 40, abs=0.5):
        pytest.fail(f"PROBE start={start} moved={moved} got={got} log={json.dumps(log)[:6000]}")
