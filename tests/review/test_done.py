import argparse
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_kinds
from tests.review.fakes import SLUG, store


@pytest.mark.feature("review-findings-queue-gate")
def test_swarm_done_refuses_a_review_that_posted_no_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(name="reviewer@a1b2c3-0002", lane="review", task="r1")
    monkeypatch.setattr(cli, "_worker", lambda *args: agent)
    monkeypatch.setattr(cli.done_gate, "require_local", lambda *args: None)
    ledger = SimpleNamespace(task=lambda *args: {"kind": "review"})
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    fields: dict[str, Any] = {f"proof_{key}": "" for key in ledger_kinds.PROOF_KEYS}
    args = argparse.Namespace(slug=SLUG, pr="", **fields)
    with pytest.raises(SwarmError, match="a review task is done only after it posts at least one finding"):
        cli.cmd_done(store(), args)
