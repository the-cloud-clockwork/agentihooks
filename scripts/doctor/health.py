"""Doctor detectors over the swarm health findings and the verdicts given them: records in, findings out."""

from scripts.doctor.words import plural
from scripts.swarm.health.findings import MINUTE_MS, Finding

UNJUDGED_MINUTES = 30


def findings(records, now_ms, unjudged_minutes=UNJUDGED_MINUTES, *, reported):
    current = {finding_id: record for finding_id, record in records.items() if finding_id in reported}
    return [*unjudged(current, now_ms, unjudged_minutes), *returned(current)]


def unjudged(records, now_ms, minutes=UNJUDGED_MINUTES):
    found = []
    for finding_id, record in sorted(records.items()):
        if record.get("verdict"):
            continue
        waited = (now_ms - record["seen_at"]) // MINUTE_MS
        if waited >= minutes:
            found.append(
                Finding(
                    "unjudged finding",
                    finding_id,
                    f"health finding without a verdict for {plural(waited, 'minute')}",
                    (f"finding {finding_id}", *record.get("evidence", [])),
                    f"{plural(minutes, 'minute')} without a verdict",
                    waited,
                )
            )
    return found


def returned(records):
    found = []
    for finding_id, record in sorted(records.items()):
        verdict = record.get("verdict")
        if not (verdict and record.get("returned")):
            continue
        found.append(
            Finding(
                "returned finding",
                finding_id,
                f"came back after a {verdict['value']} verdict",
                (
                    f"verdict {verdict['value']} by {verdict['by']}: {verdict['note']}",
                    f"measure {verdict['measure']} at the verdict, {record['measure']} now",
                    *record.get("evidence", []),
                ),
                "a judged finding shown again after its cooldown with grown evidence",
                record["measure"] - verdict["measure"],
            )
        )
    return found
