"""GitHub's own status page: the unresolved incidents that affect Actions."""

import json
import re
import urllib.request

from scripts.swarm.ledger_events import iso_ms

URL = "https://www.githubstatus.com/api/v2/incidents/unresolved.json"
COMPONENT = "Actions"
KEEP_MS = 86_400_000


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


def _actions(incident: dict) -> bool:
    named = any(component.get("name") == COMPONENT for component in incident.get("components") or [])
    return named or re.search(rf"\b{COMPONENT}\b", incident["name"]) is not None


def unresolved() -> list[dict] | None:
    """The open incidents affecting Actions, or None when the status page cannot be read."""
    try:
        body = json.loads(_fetch(URL))
        return [
            {
                "id": incident["id"],
                "name": incident["name"],
                "start": iso_ms(incident.get("started_at") or incident["created_at"]),
                "link": incident.get("shortlink") or "",
            }
            for incident in body["incidents"]
            if _actions(incident)
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def track(known: dict, current: list[dict] | None, now_ms: int) -> dict:
    """Opens each listed incident and closes those a read no longer lists; an unread status changes nothing."""
    kept = {
        identity: dict(incident)
        for identity, incident in known.items()
        if incident["end"] is None or now_ms - incident["end"] < KEEP_MS
    }
    if current is None:
        return kept
    listed = {incident["id"] for incident in current}
    for identity, incident in kept.items():
        if incident["end"] is None and identity not in listed:
            incident["end"] = now_ms
    for incident in current:
        kept[incident["id"]] = {"noticed": False, **kept.get(incident["id"], {}), **incident, "end": None}
    return kept


def windows(known: dict, now_ms: int) -> list[tuple[int, int, str]]:
    return [(incident["start"], incident["end"] or now_ms, incident["name"]) for incident in known.values()]
