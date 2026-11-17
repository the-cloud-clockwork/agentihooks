"""A swarm control command hands the control notice the master the swarm had before the control ran."""

from typing import Any

import pytest

from scripts.swarm import control_notifications
from tests.swarm.test_cli import env, run  # noqa: F401


def test_a_control_command_hands_the_notice_the_master_found_before_it(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead = object()
    noticed: list[tuple[object, str]] = []

    def master(store: object, slug: str) -> object:
        return lead

    def notify(store: object, args: object, ledger: object, before: object, action: str, detail: str = "") -> None:
        noticed.append((before, action))

    run("sw", "create", "--repo", "/repo")
    monkeypatch.setattr(control_notifications, "master", master)
    monkeypatch.setattr(control_notifications, "notify", notify)
    assert run("sw", "pause") == 0
    assert noticed == [(lead, "pause")]
