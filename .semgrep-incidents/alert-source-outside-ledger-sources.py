from typing import Any

from scripts.swarm_ledger import ledger_alerts
from scripts.swarm_ledger.ledger_alerts import MERGE, SEAT

# ruleid: alert-source-outside-ledger-sources
ALERT_SOURCE = "requeue"
# ruleid: alert-source-outside-ledger-sources
LOGIN_ALERT_SOURCE = "master-login"
# ok: alert-source-outside-ledger-sources
MERGE_ALERT_SOURCE = MERGE
# ok: alert-source-outside-ledger-sources
SEAT_ALERT_SOURCE = ledger_alerts.SEAT


def requeue_failed(ledger: Any, slug: str, url: str, why: str) -> None:
    # ruleid: alert-source-outside-ledger-sources
    ledger.raise_alert(slug, f"merge-priority-requeue:{url}", f"Could not requeue {url}: {why}")
    # ruleid: alert-source-outside-ledger-sources
    ledger.raise_alert(slug, "master-login", "The master login reached its limit")
    # ok: alert-source-outside-ledger-sources
    ledger.raise_alert(slug, MERGE, f"Could not requeue {url}: {why}")
    # ok: alert-source-outside-ledger-sources
    ledger.raise_alert(slug, SEAT, "The master login reached its limit")
