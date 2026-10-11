"""Fleet dashboard panels and bounded alerts projected from recorded observations; an alert recommends, never acts."""

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import telemetry

DEFINITION = Path(__file__).resolve().parents[2] / "observability" / "swarm-v2.json"
ALERTS_ENV = "AGENTIHOOKS_FLEET_ALERTS"
INCIDENTS = "fleet-incidents"
EVALUATED = "fleet-incidents-seq"
WITHHELD = "[withheld outside the viewer's brain grant]"
OPEN, RESOLVED = "open", "resolved"
ACTIVE, PENDING = "active", "pending"
WRITE_ATTEMPTS = 5


class ViewRefused(SwarmError):
    """A viewer grant that names no brain list; a display label never stands in for one."""


@dataclass(frozen=True)
class Viewer:
    actor: str
    brains: frozenset[str]


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    for_ms: int
    limit: int
    owner: str
    action: str


def viewer(grant: Mapping) -> Viewer:
    brains = grant.get("brains")
    if not isinstance(brains, list) or not all(isinstance(brain, str) and brain for brain in brains):
        raise ViewRefused("a viewer grant names its brains as a list of brain ids")
    return Viewer(telemetry.sanitize_text(str(grant.get("actor", ""))), frozenset(brains))


def definition(path: Path = DEFINITION) -> dict:
    return json.loads(path.read_text())


def rules(found: dict) -> tuple[Rule, ...]:
    return tuple(Rule(**row) for row in found["alerts"])


def alerts_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    value = environ.get(ALERTS_ENV)
    return value is None or value.strip().lower() != "off"


def _series(found: dict, panel: str, values: dict) -> dict:
    labels = next(row["series"] for row in found["panels"] if row["id"] == panel)
    return {key: {"label": labels[key], "value": values[key]} for key in labels}


def _attempts(attempts: list[dict]) -> dict:
    phases = Counter(row["phase"] for row in attempts)
    return {"active": phases[ACTIVE], "pending": phases[PENDING]}


def _capacity(found: dict, pools: list[dict], attempts: list[dict]) -> list[dict]:
    rows = []
    for pool in pools:
        mine = [row for row in attempts if row.get("pool") == pool["pool"]]
        values = {
            "configured_maximum": pool["configured_maximum"],
            "observed_nodes": pool["nodes"],
            "active_tasks": sum(row["phase"] == ACTIVE for row in mine),
            "pending_requests": sum(row["phase"] == PENDING for row in mine),
        }
        rows.append({"pool": pool["pool"], "series": _series(found, "capacity", values)})
    return rows


def _quota(quota: list[dict], at: int) -> dict:
    waits = [at - row["since_ms"] for row in quota if row["state"] == "waiting"]
    return {"waiting_accounts": len(waits), "longest_wait_ms": max(waits, default=0)}


def _archive(archive: list[dict], at: int, limit: int) -> dict:
    lags = [at - row["advanced_at_ms"] for row in archive]
    return {"lagging": sum(lag > limit for lag in lags), "worst_lag_ms": max(lags, default=0)}


def _checkpoints(checkpoints: list[dict], at: int, limit: int) -> dict:
    ages = [at - row["taken_at_ms"] for row in checkpoints]
    return {"stale": sum(age > limit for age in ages), "oldest_age_ms": max(ages, default=0)}


def _connectivity(attempts: list[dict]) -> dict:
    return {
        "terminal": dict(sorted(Counter(row["terminal"] for row in attempts).items())),
        "authority": dict(sorted(Counter(row["authority"] for row in attempts).items())),
    }


def _denial(row: dict, seen: Viewer) -> dict:
    granted = row["target_brain"] in seen.brains
    return {
        "denial_id": telemetry.sanitize_text(str(row["denial_id"])),
        "at_ms": row["at_ms"],
        "requester_scope": telemetry.sanitize_text(str(row["requester_scope"])),
        "target_brain": telemetry.sanitize_text(str(row["target_brain"])) if granted else WITHHELD,
        "session_title": telemetry.sanitize_text(str(row["session_title"])) if granted else WITHHELD,
    }


def dashboard(sample: dict, seen: Viewer, found: dict) -> dict:
    """One compact view per sample; a private session title shows only to a viewer granted its brain."""
    at, limits, attempts = sample["at_ms"], found["limits"], sample["attempts"]
    brain = sample["brain"]
    knowledge = {"feed_age_ms": at - brain["feed_at_ms"], "cross_scope_denials": len(brain["denials"])}
    return {
        "seq": sample["seq"],
        "at_ms": at,
        "viewer": seen.actor,
        "panels": {
            "attempts": _series(found, "attempts", _attempts(attempts)),
            "capacity": _capacity(found, sample["pools"], attempts),
            "quota_wait": _series(found, "quota_wait", _quota(sample["quota"], at)),
            "archive_lag": _series(found, "archive_lag", _archive(sample["archive"], at, limits["archive_lag_ms"])),
            "checkpoint_age": _series(
                found, "checkpoint_age", _checkpoints(sample["checkpoints"], at, limits["checkpoint_stale_ms"])
            ),
            "connectivity": _series(found, "connectivity", _connectivity(attempts)),
            "knowledge": {
                **_series(found, "knowledge", knowledge),
                "stale": knowledge["feed_age_ms"] > limits["knowledge_stale_ms"],
                "denials": [_denial(row, seen) for row in brain["denials"]],
            },
        },
    }


def _fits(requested: dict | None, template: dict | None) -> bool:
    """An unknown request or template cannot prove a request impossible."""
    if requested is None or template is None:
        return True
    return requested["cpu_millis"] <= template["cpu_millis"] and requested["memory_mib"] <= template["memory_mib"]


def _impossible(attempt: dict, pools: dict[str, dict]) -> str:
    permitted = [pools[name] for name in attempt["permitted_pools"] if name in pools]
    fitting = [pool for pool in permitted if _fits(attempt.get("requested"), pool.get("template"))]
    if not fitting:
        return "no_permitted_template_fits"
    if all(pool["nodes"] >= pool["configured_maximum"] for pool in fitting):
        return "permitted_pools_at_configured_maximum"
    return ""


def _conditions(sample: dict) -> list[tuple[str, str, int, dict]]:
    pools = {pool["pool"]: pool for pool in sample["pools"]}
    found = []
    for row in sample["attempts"]:
        if row["phase"] == ACTIVE and row.get("protected") and row.get("idle_since_ms") is not None:
            facts = {"execution_id": row["execution_id"], "seat": row["seat"], "pool": row.get("pool")}
            found.append(("protected_idle_pod", row["execution_id"], row["idle_since_ms"], facts))
        reason = _impossible(row, pools) if row["phase"] == PENDING else ""
        if reason:
            facts = {"execution_id": row["execution_id"], "seat": row["seat"], "reason": reason}
            found.append(("impossible_pending", row["execution_id"], row["pending_since_ms"], facts))
    for row in sample["brain"]["denials"]:
        facts = {"denial_id": row["denial_id"], "requester_scope": row["requester_scope"]}
        found.append(("cross_scope_denial", row["denial_id"], row["at_ms"], facts))
    return found


def fingerprint(rule: str, subject: str) -> str:
    return hashlib.sha256(f"{rule}\t{subject}".encode()).hexdigest()[:16]


def evaluate(sample: dict, found: dict, environ: Mapping[str, str] = os.environ) -> dict:
    """Alerts whose condition has held for its rule's window, at most limit per rule; session titles never enter."""
    if not alerts_enabled(environ):
        disabled = {"state": "disabled", "alerts": [], "held": [], "suppressed": {}}
        return {"seq": sample["seq"], "at_ms": sample["at_ms"], **disabled}
    by_id = {rule.id: rule for rule in rules(found)}
    held: dict[str, list[dict]] = {rule: [] for rule in by_id}
    for rule_id, subject, since, facts in _conditions(sample):
        rule = by_id[rule_id]
        duration = sample["at_ms"] - since
        if duration < rule.for_ms:
            continue
        clean = {key: telemetry.sanitize_text(str(value)) for key, value in facts.items()}
        held[rule_id].append(
            {
                "fingerprint": fingerprint(rule_id, subject),
                "rule": rule_id,
                "severity": rule.severity,
                "subject": telemetry.sanitize_text(subject),
                "held_ms": duration,
                "facts": clean,
                "owner": rule.owner,
                "action": rule.action,
                "automatic": False,
            }
        )
    alerts, suppressed = [], {}
    for rule_id, rows in held.items():
        rows.sort(key=lambda row: (-row["held_ms"], row["subject"]))
        alerts.extend(rows[: by_id[rule_id].limit])
        suppressed[rule_id] = max(0, len(rows) - by_id[rule_id].limit)
    return {
        "seq": sample["seq"],
        "at_ms": sample["at_ms"],
        "state": "enabled",
        "alerts": alerts,
        "held": sorted(row["fingerprint"] for rows in held.values() for row in rows),
        "suppressed": suppressed,
    }


def alert_actionability_review(evaluated: dict, found: dict) -> dict:
    """An alert is actionable when it carries its rule's owner and action, names its subject and never acts itself."""
    by_id = {rule.id: rule for rule in rules(found)}
    unactionable = [
        f"{alert['rule']}:{alert['subject']}"
        for alert in evaluated["alerts"]
        if (alert["owner"], alert["action"]) != (by_id[alert["rule"]].owner, by_id[alert["rule"]].action)
        or not (alert["owner"] and alert["action"])
        or alert["automatic"]
        or alert["subject"] not in alert["facts"].values()
    ]
    return {
        "state": evaluated["state"],
        "alerts": len(evaluated["alerts"]),
        "actionable": len(evaluated["alerts"]) - len(unactionable),
        "unactionable": unactionable,
        "by_rule": dict(sorted(Counter(alert["rule"] for alert in evaluated["alerts"]).items())),
        "suppressed": dict(sorted(evaluated["suppressed"].items())),
    }


class IncidentStore:
    """One incident per fingerprint; a replayed or older evaluation changes nothing, and delivery only notifies."""

    def __init__(self, store: RedisStore, slug: str) -> None:
        self.store, self.slug = store, slug
        self.key = store.key(slug, INCIDENTS)
        self.seq_key = store.key(slug, EVALUATED)

    def incidents(self) -> dict[str, dict]:
        return {fp: json.loads(raw) for fp, raw in self.store.redis.hgetall(self.key).items()}

    def record(self, evaluated: dict) -> dict:
        seq = evaluated["seq"]
        if evaluated["state"] != "enabled":
            return {"seq": seq, "stale": False, "disabled": True, "opened": [], "resolved": []}
        return self._retrying(self._record, seq, evaluated["alerts"], set(evaluated["held"]))

    def _retrying(self, write: Callable[..., dict | None], *args: object) -> dict | None:
        from redis.exceptions import WatchError

        for _ in range(WRITE_ATTEMPTS):
            try:
                return write(*args)
            except WatchError:
                continue
        raise SwarmError("incident store kept changing; the write was not recorded")

    def _record(self, seq: int, alerts: list[dict], held: set[str]) -> dict:
        """Only the capped alerts open incidents; an open incident resolves only when nothing holds it."""
        with self.store.redis.pipeline() as pipe:
            pipe.watch(self.key, self.seq_key)
            if seq <= int(pipe.get(self.seq_key) or 0):
                pipe.unwatch()
                return {"seq": seq, "stale": True, "disabled": False, "opened": [], "resolved": []}
            current = {fp: json.loads(raw) for fp, raw in pipe.hgetall(self.key).items()}
            writes, opened, resolved = {}, [], []
            for alert in alerts:
                fp, known = alert["fingerprint"], current.get(alert["fingerprint"])
                if known is None or known["state"] == RESOLVED:
                    keep = {key: alert[key] for key in ("rule", "severity", "subject", "owner", "action")}
                    opening = {"state": OPEN, "opened_seq": seq, "last_seq": seq, "notified": False}
                    writes[fp] = {**keep, "facts": alert["facts"], **opening}
                    opened.append(fp)
                else:
                    writes[fp] = {**known, "facts": alert["facts"], "last_seq": seq}
            for fp, known in current.items():
                if known["state"] == OPEN and fp not in held:
                    writes[fp] = {**known, "state": RESOLVED, "resolved_seq": seq}
                    resolved.append(fp)
            pipe.multi()
            if writes:
                pipe.hset(self.key, mapping={fp: json.dumps(row) for fp, row in writes.items()})
            pipe.set(self.seq_key, seq)
            pipe.execute()
        return {"seq": seq, "stale": False, "disabled": False, "opened": sorted(opened), "resolved": sorted(resolved)}

    def deliver(self, sink: Callable[[dict], None], environ: Mapping[str, str] = os.environ) -> int:
        """Hands each opened incident to sink once; a failing sink leaves the rest for the next call."""
        if not alerts_enabled(environ):
            return 0
        sent = 0
        for fp, row in sorted(self.incidents().items(), key=lambda item: (item[1]["opened_seq"], item[0])):
            if row["state"] != OPEN or row["notified"]:
                continue
            shown = ("rule", "severity", "subject", "facts", "owner", "action")
            sink({"incident_id": f"{fp}:{row['opened_seq']}", **{key: row[key] for key in shown}})
            self._retrying(self._notified, fp, row["opened_seq"])
            sent += 1
        return sent

    def _notified(self, fp: str, opened_seq: int) -> None:
        with self.store.redis.pipeline() as pipe:
            pipe.watch(self.key)
            raw = pipe.hget(self.key, fp)
            row = json.loads(raw) if raw else None
            if row is None or row["opened_seq"] != opened_seq:
                pipe.unwatch()
                return
            pipe.multi()
            pipe.hset(self.key, fp, json.dumps({**row, "notified": True}))
            pipe.execute()
