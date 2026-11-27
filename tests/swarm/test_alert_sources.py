"""Every swarm alert raiser sends a source the ledger server accepts."""

import pytest

from scripts.swarm import master_prompt_stall, queue_priority, ready
from scripts.swarm.ledger_client import LedgerRefused, _op
from scripts.swarm_ledger import ledger_alerts


class CheckedLedger:
    """A ledger fake that refuses an alert the server's raise check refuses, as the hosted ledger does."""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str]] = []

    def raise_alert(self, slug: str, source: str, text: str, item: str | None = None) -> None:
        fields = {"item": item} if item else {}
        try:
            ledger_alerts.check_raise(_op("alert_raise", "swarm", source=source, text=text, **fields))
        except ValueError as exc:
            raise LedgerRefused(f"ledger {slug}: server refused: 400 schema_invalid {exc}") from exc
        self.alerts.append((slug, source, text))


@pytest.mark.feature("swarm-alert-sources-accepted")
@pytest.mark.parametrize(
    "source",
    [ready.ALERT_SOURCE, queue_priority.ALERT_SOURCE, master_prompt_stall.LOGIN_ALERT_SOURCE],
    ids=["ready-requeue", "queue-priority-requeue", "master-login"],
)
def test_the_ledger_accepts_each_swarm_alert_source(source: str) -> None:
    ledger = CheckedLedger()
    ledger.raise_alert("sw", source, "Could not requeue https://github.com/o/r/pull/2: GitHub unavailable")
    assert ledger.alerts == [("sw", source, "Could not requeue https://github.com/o/r/pull/2: GitHub unavailable")]
