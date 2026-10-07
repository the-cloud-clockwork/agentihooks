import argparse

import pytest

from scripts.swarm import cli, command_runner
from tests.inbox.test_wake import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm_ledger.test_caps_columns import browser
from tests.swarm_ledger.test_remote_control_process import remote_server

pytestmark = pytest.mark.xdist_group("fakeredis")
__all__ = ["browser", "remote_server"]


def test_page_keeps_pending_and_acknowledged_control_states(remote_server, monkeypatch, tmp_path, browser):
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
        page.wait_for_function(
            "document.querySelector('#swarm-note').textContent === 'Pause: pending, waiting for the hive tick'"
        )
        page.locator("#command-log summary").click()
        page.screenshot(path=str(tmp_path / "pending.png"))
        assert request("GET")["commands"][0]["state"] == "pending"
        cli.run_tick(saved, "sw", ledger, FakeRuntime(), FakeHerdr({}))
        page.reload()
        page.wait_for_function("document.querySelector('#swarm-note').textContent === 'Pause: acknowledged'")
        page.screenshot(path=str(tmp_path / "acknowledged.png"))
        assert errors == []
