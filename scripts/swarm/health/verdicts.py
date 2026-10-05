"""Health finding verdicts: who judged a finding, how, and whether it shows again after its cooldown."""

import json

from scripts.swarm.store import SwarmError

VERDICTS = ("false-positive", "early-real", "established", "insufficient-evidence", "resolved")


class VerdictStore:
    def __init__(self, redis, key):
        self.redis, self.key = redis, key

    def visible(self, found, now_ms, cooldown_ms):
        stored = self.redis.hgetall(self.key)
        shown = []
        for finding in found:
            before = json.loads(stored[finding.id]) if finding.id in stored else None
            record = {**(before or {"seen_at": now_ms, "verdict": None, "returned": False})}
            record.update(evidence=list(finding.evidence), measure=finding.measure)
            verdict = record["verdict"]
            show = verdict is None or record["returned"]
            if verdict and not show and now_ms >= verdict["at"] + cooldown_ms and finding.measure > verdict["measure"]:
                record["returned"] = show = True
            if record != before:
                self.redis.hset(self.key, finding.id, json.dumps(record))
            if show:
                earlier = {k: verdict[k] for k in ("value", "note", "by", "at")} if verdict else None
                shown.append({"id": finding.id, **finding.as_dict(), "verdict": earlier})
        return shown

    def judge(self, finding_id, value, note, by, now_ms):
        if value not in VERDICTS:
            raise SwarmError(f"a verdict is one of {', '.join(VERDICTS)}")
        raw = self.redis.hget(self.key, finding_id)
        if raw is None:
            raise SwarmError(f"no finding {finding_id}; swarm status lists the findings and their ids")
        record = json.loads(raw)
        verdict = {"value": value, "note": note, "by": by, "at": now_ms, "measure": record["measure"]}
        record.update(verdict=verdict, returned=False)
        self.redis.hset(self.key, finding_id, json.dumps(record))
        return verdict
