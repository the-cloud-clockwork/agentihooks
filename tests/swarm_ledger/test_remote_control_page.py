import argparse

import pytest
from playwright.sync_api import expect

from scripts.swarm import cli, command_runner
from tests.inbox.test_wake import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm_ledger.test_caps_columns import browser
from tests.swarm_ledger.test_remote_control_process import remote_server

__all__ = ["browser", "remote_server"]


def test_page_shows_no_note_text_for_pending_or_acknowledged_control_states(
    remote_server, monkeypatch, tmp_path, browser
):
    saved, request, url = remote_server
    ledger = FakeLedger([])
    command_runner.publish(saved, "sw", ledger.state("sw"))
    monkeypatch.setattr(
        command_runner, "run", lambda argv, env: cli.cmd_pause(saved, argparse.Namespace(slug="sw")) or ""
    )
    with browser.new_context(viewport={"width": 1920, "height": 1080}) as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/sw#swarm")
        page.locator('[data-swarm="pause"]').click()
        expect(page.locator("#swarm-note")).to_have_text("")
        page.screenshot(path=str(tmp_path / "pending.png"))
        assert request("GET")["commands"][0]["state"] == "pending"
        cli.run_tick(saved, "sw", ledger, FakeRuntime(), FakeHerdr({}))
        page.reload()
        expect(page.locator("#swarm-note")).to_have_text("")
        page.screenshot(path=str(tmp_path / "acknowledged.png"))
        assert errors == []


@pytest.mark.parametrize("refusal", ["", "the controller lease is stale"])
def test_page_full_autonomy_shows_current_config_or_its_refusal(remote_server, monkeypatch, browser, refusal):
    saved, request, url = remote_server
    saved.update("sw", state="paused", autonomy="delegate")
    command_runner.publish(saved, "sw", FakeLedger([]).state("sw"))

    def run(argv, env):
        assert argv == ["swarm", "sw", "set", "autonomy=full"]
        if refusal:
            return refusal
        cli.cmd_set(saved, argparse.Namespace(slug="sw", pairs=["autonomy=full"]))
        return ""

    monkeypatch.setattr(command_runner, "run", run)
    with browser.new_context(viewport={"width": 1920, "height": 1080}) as context:
        page = context.new_page()
        page.goto(url + "/sw#swarm")
        page.locator('[data-autonomy="full"]').click()
        expect(page.locator('[data-autonomy="full"]')).to_be_enabled()
        assert request("GET")["commands"][-1]["argv"] == ["set", "autonomy=full"]
        command_runner.consume(saved, "sw")
        page.reload()
        if refusal:
            assert request("GET")["config"]["autonomy"] == "delegate"
            expect(page.locator('[data-autonomy="delegate"]')).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#swarm-note")).to_have_text(
                "Could not set autonomy: the controller lease is stale. Try again or ask the master."
            )
        else:
            assert request("GET")["config"]["autonomy"] == "full"
            expect(page.locator('[data-autonomy="full"]')).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#swarm-note")).to_have_text("")
