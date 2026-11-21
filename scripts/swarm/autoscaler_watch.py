"""Raise one ledger alert to the masters when a cluster autoscaler node group fails to scale up twice in a row, or
when an instance it launched misses its join budget, read from the autoscaler status ConfigMap."""

from typing import Any

from scripts.swarm_v2.kubernetes.client import KubeHttp as KubeHttp

SETTING = "AGENTIHOOKS_AUTOSCALER_STATUS"
RECOVERED = ""


def alert_pass(slug: str, store: Any, ledger: Any, doc: dict[str, Any]) -> list[str]:
    """Return the actions of one autoscaler watch pass."""
    return []
