from unittest.mock import Mock

import pytest

from tests.hub.test_github_webhooks import deliver, drain, lab_pull_request, lab_setup

pytestmark = pytest.mark.feature("lab-head-before-pull-observation")


def test_a_failed_pull_observation_still_publishes_the_lab_head(monkeypatch: pytest.MonkeyPatch) -> None:
    ingress, swarm, lab = lab_setup()
    observe = Mock(side_effect=ConnectionError("pull observation failed"))
    monkeypatch.setattr("scripts.hub.github_webhooks.Pulls.observe", observe)

    assert deliver(ingress, "pull_request", lab_pull_request()) == 503
    observe.assert_called_once()
    assert [(event.type, event.data["head"], event.data["action"]) for event in drain(lab)] == [
        ("ci.lab.head.pull_request", "a1", "opened")
    ]
    assert drain(swarm) == []
