import datetime
import json

import pytest

from tests.swarm_ledger import page_replay
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser


class LateOutline:
    def __init__(self):
        self.delivered = False
        self.polls = 0

    def evaluate(self, script):
        if script == page_replay.FLUSH:
            self.polls = 0
            return None
        if script == page_replay.FLUSHED:
            self.polls += 1
            self.delivered = self.delivered or self.polls == 2
            return self.polls >= 2
        return [0, "outline marked" if self.delivered else "outline pending"]


class StalledOutline:
    def __init__(self):
        self.captures = 0

    def evaluate(self, script):
        if script == page_replay.FLUSHED:
            return False
        if script != page_replay.FLUSH:
            self.captures += 1
        return None


def test_quiet_drains_a_real_observer_delivery_under_the_paused_clock(browser):
    page = browser.new_page()
    try:
        page.clock.install(time=page_replay.START - datetime.timedelta(seconds=10))
        page.clock.pause_at(page_replay.START)
        page.route(
            "http://replay.test/",
            lambda route: route.fulfill(
                content_type="text/html",
                body='<main id="main-content"><a id="link">Operator notes</a></main><script>window.__inflight = 0;'
                'new IntersectionObserver(() => { document.getElementById("link").className = "on"; })'
                '.observe(document.getElementById("link"));</script>',
            ),
        )
        page.goto("http://replay.test/")
        now = page_replay.quiet(page)
    finally:
        page.close()
    assert '<a id="link" class="on">' in json.loads(now[1])["html"]


def test_quiet_waits_for_pending_outline_deliveries_before_capture():
    assert page_replay.quiet(LateOutline()) == [0, "outline marked"]


def test_quiet_times_out_instead_of_capturing_while_a_delivery_stays_pending(monkeypatch):
    monkeypatch.setattr(page_replay.time, "sleep", lambda seconds: None)
    page = StalledOutline()
    with pytest.raises(TimeoutError):
        page_replay.quiet(page)
    assert page.captures == 0
