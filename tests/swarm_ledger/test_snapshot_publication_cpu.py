from unittest.mock import Mock, patch

import pytest

from scripts.swarm_ledger import ledger_server


@pytest.mark.parametrize("subscribed", [False, True])
def test_snapshot_publication_only_builds_views_for_subscribed_ledgers(subscribed):
    state = {"revision": 7}
    view = {"revision": 7, "tasks": []}
    hub = Mock()
    hub.has.return_value = subscribed
    with (
        patch.object(ledger_server, "HUB", hub),
        patch.object(ledger_server, "ledger_view", return_value=view) as render,
    ):
        ledger_server.publish_snapshot("cpu-proof", state)

    hub.has.assert_called_once_with("cpu-proof")
    if subscribed:
        render.assert_called_once_with(state, "cpu-proof")
        assert render.call_args.args[0] is state
        hub.publish_snapshot.assert_called_once_with("cpu-proof", "ledger", view)
        assert hub.publish_snapshot.call_args.args[2] is view
    else:
        render.assert_not_called()
        hub.publish_snapshot.assert_not_called()
