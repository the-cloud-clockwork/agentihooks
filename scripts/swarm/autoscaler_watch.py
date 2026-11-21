"""Raise one ledger alert to the masters when a cluster autoscaler node group fails to scale up twice in a row, or
when an instance it launched misses its join budget, read from the autoscaler status ConfigMap."""

import json
import os
from typing import Any

import yaml

from scripts.swarm_ledger.ledger_alerts import AUTOSCALER, MAX_OUTCOME
from scripts.swarm_v2.kubernetes.client import KubeHttp

SETTING = "AGENTIHOOKS_AUTOSCALER_STATUS"
RECOVERED = "every node group recovered"
KEY = "autoscaler-watch"
TIMEOUT = "timeout"
FAILURES = (OSError, KeyError, TypeError, ValueError, yaml.YAMLError)
FRESH: dict[str, Any] = {"count": 0, "pair": None, "ready": 0, "late": 0, "reason": "none"}
ALERT = (
    "Cluster autoscaler node group {name} (min {low}, max {high} nodes) has {count} failed scale ups in a row, last "
    "failure: {reason}. {ready} of {target} target nodes are ready and {late} launched instances are past the join "
    "budget."
)


def reading(group: dict[str, Any]) -> dict[str, Any]:
    """Return what the watch reads of one status node group: its name, sizes, target, ready and late node counts
    and, while its scale up backs off, the failure's transition time, message and code."""
    health = group["health"]
    counts = health["nodeCounts"]
    found: dict[str, Any] = {
        "name": group["name"],
        "low": health["minSize"],
        "high": health["maxSize"],
        "target": health["cloudProviderTarget"],
        "ready": counts["registered"]["ready"],
        "late": counts["longUnregistered"],
        "failure": None,
    }
    scale_up = group["scaleUp"]
    if scale_up["status"] == "Backoff":
        info = scale_up["backoffInfo"]
        found["failure"] = [scale_up["lastTransitionTime"], info["errorMessage"], info["errorCode"]]
    return found


def node_groups(value: str) -> tuple[list[dict[str, Any]], str]:
    """Return the node groups of the status ConfigMap that ``value`` names as namespace and name joined by a slash,
    or no groups and why the read failed."""
    try:
        namespace, name = value.split("/")
        path = f"/api/v1/namespaces/{namespace}/configmaps/{name}"
        code, body = KubeHttp.in_cluster(os.environ).send("GET", path)  # pyright: ignore[reportUnknownMemberType]
        if code != 200:
            return [], f"the API server answered {code}"
        return [reading(group) for group in yaml.safe_load(body["data"]["status"])["nodeGroups"]], ""
    except FAILURES as error:
        return [], f"{type(error).__name__}: {error}"


def counted(found: dict[str, Any], prior: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return the node group's record after this read and whether the read brought new evidence: a newly counted
    failure that makes two in a row or timed out, or more launched instances past the join budget than before. A rise
    in ready nodes resets the count."""
    record = {**prior, "ready": found["ready"], "late": found["late"]}
    if found["ready"] > prior["ready"]:
        record["count"] = 0
    failure: list[str] | None = found["failure"]
    if failure is None or failure[:2] == prior["pair"]:
        return record, found["late"] > prior["late"]
    record.update(count=record["count"] + 1, pair=failure[:2], reason=f"{failure[2]}: {failure[1]}")
    return record, record["count"] >= 2 or failure[2] == TIMEOUT or found["late"] > prior["late"]


def settle(slug: str, ledger: Any, doc: dict[str, Any], raised: dict[str, str], recovered: bool) -> list[str]:
    """Raise one autoscaler alert for the groups with new evidence while none is open, or close the open one once
    every group recovered, and return what was done."""
    alerts: list[dict[str, Any]] = doc.get("alerts") or []
    live = [a["id"] for a in alerts if a.get("source") == AUTOSCALER and a.get("state") != "done"]
    if raised and not live:
        ledger.raise_alert(slug, AUTOSCALER, " ".join(raised.values())[:MAX_OUTCOME])
        return [f"raised the autoscaler alert for {', '.join(raised)}"]
    if live and recovered:
        for alert_id in live:
            ledger.close_alert(slug, alert_id, RECOVERED)
        return [f"closed the autoscaler alert {alert_id}: {RECOVERED}" for alert_id in live]
    return []


def alert_pass(slug: str, store: Any, ledger: Any, doc: dict[str, Any]) -> list[str]:
    """Return the actions of one autoscaler watch pass: count each node group's failed scale ups in a row in the
    swarm Redis, raise the autoscaler alert on new evidence and close it once every group recovered. An unset setting
    reads nothing and a failed read changes nothing."""
    value = os.environ.get(SETTING)
    if not value:
        return []
    groups, why = node_groups(value)
    if why:
        return [f"read no autoscaler status from {value}: {why}"]
    key = store.key(slug, KEY)
    prior = json.loads(store.redis.get(key) or "{}")
    records: dict[str, dict[str, Any]] = {}
    raised: dict[str, str] = {}
    recovered = True
    for found in groups:
        record, new = counted(found, prior.get(found["name"], FRESH))
        records[found["name"]] = record
        if new:
            raised[found["name"]] = ALERT.format(**found, count=record["count"], reason=record["reason"])
        recovered = recovered and not found["failure"] and found["late"] == 0 and record["count"] == 0
    actions = settle(slug, ledger, doc, raised, recovered)
    store.redis.set(key, json.dumps(records))
    return actions
