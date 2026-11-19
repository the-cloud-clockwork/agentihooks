"""Tests for scripts/install.py's main() dispatch of the ledger and api programs."""

from unittest.mock import MagicMock

import pytest

from scripts import install


@pytest.mark.parametrize("prog", ["ledger", "api"])
def test_main_dispatches_ledger_and_api_by_their_own_invoked_name(prog: str, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock(return_value=7)
    monkeypatch.setattr("scripts.swarm_ledger.run", mock_run)
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", prog, "subcmd"])
    with pytest.raises(SystemExit) as raised:
        install.main()
    assert raised.value.code == 7
    mock_run.assert_called_once_with(["subcmd"], prog=prog)
