"""Health finding verdicts: who judged a finding, how, and whether it shows again after its cooldown."""

import json
from math import inf
from uuid import uuid4

from scripts.swarm.store import SwarmError

VERDICTS = ("false-positive", "early-real", "established", "insufficient-evidence", "resolved")


class VerdictStore:
    HISTORY_MS = 7 * 86400000

    def __init__(self, redis, key, event_key=""):
        self.redis, self.key = redis, key
        self.event_key = event_key

    def visible(self, found, now_ms, cooldown_ms, new_evidence=False):
        stored = self.redis.hgetall(self.key)
        shown = []
        for finding in found:
            before = json.loads(stored[finding.id]) if finding.id in stored else None
            record = {**(before or {"seen_at": now_ms, "verdict": None, "returned": False})}
            record.update(evidence=list(finding.evidence), measure=finding.measure)
            if finding.basis:
                record["basis"] = finding.basis
            verdict = record["verdict"]
            if finding.evidence_identity is not None:
                record["evidence_identity"] = list(finding.evidence_identity)
                if verdict and "evidence_identity" not in verdict:
                    verdict = {**verdict, "evidence_identity": record["evidence_identity"]}
                    record["verdict"] = verdict
            if verdict and verdict.get("basis", "") != finding.basis:
                rebase = {"measure": verdict["measure"], "basis": verdict.get("basis", ""), "at": now_ms}
                verdict = {**verdict, "measure": finding.measure, "basis": finding.basis}
                verdict["rebases"] = [*verdict.get("rebases", []), rebase]
                record["verdict"] = verdict
            show = verdict is None or record["returned"]
            grew = verdict and finding.measure > verdict["measure"]
            if grew and new_evidence:
                key = "evidence_identity" if finding.evidence_identity is not None else "evidence"
                grew = record[key] != verdict.get(key, before.get(key))
            elif verdict and finding.evidence_identity is not None and not new_evidence:
                grew = record["evidence_identity"] != verdict["evidence_identity"]
            if grew and not show and now_ms >= verdict["at"] + cooldown_ms:
                record["returned"] = show = True
            if record != before:
                self.redis.hset(self.key, finding.id, json.dumps(record))
            if show:
                earlier = {k: verdict[k] for k in ("value", "note", "by", "at")} if verdict else None
                shown.append({"id": finding.id, **finding.as_dict(), "verdict": earlier, "seen_at": record["seen_at"]})
        return shown

    def judge(self, finding_id, value, note, by, now_ms):
        if value not in VERDICTS:
            raise SwarmError(f"a verdict is one of {', '.join(VERDICTS)}")
        raw = self.redis.hget(self.key, finding_id)
        if raw is None:
            raise SwarmError(f"no finding {finding_id}; swarm status lists the findings and their ids")
        record = json.loads(raw)
        verdict = {
            "value": value,
            "note": note,
            "by": by,
            "at": now_ms,
            "measure": record["measure"],
            "evidence": record["evidence"],
        }
        if "evidence_identity" in record:
            verdict["evidence_identity"] = record["evidence_identity"]
        if record.get("basis"):
            verdict["basis"] = record["basis"]
        prior = record.get("verdict")
        if prior:
            earlier = [*prior.get("earlier", []), {"at": prior["at"], "measure": prior["measure"]}]
            verdict["earlier"] = [entry for entry in earlier if entry["at"] >= now_ms - self.HISTORY_MS]
        record.update(verdict=verdict, returned=False)
        self._save(finding_id, record, verdict)
        return verdict

    def _save(self, finding_id, record, verdict):
        if not self.event_key:
            self.redis.hset(self.key, finding_id, json.dumps(record))
            return
        event = {key: verdict[key] for key in ("value", "note", "by", "at", "measure")}
        event["finding"] = finding_id
        member = json.dumps([uuid4().hex, event])
        with self.redis.pipeline() as pipe:
            pipe.hset(self.key, finding_id, json.dumps(record))
            pipe.zadd(self.event_key, {member: verdict["at"]})
            pipe.zremrangebyscore(self.event_key, -inf, f"({verdict['at'] - self.HISTORY_MS}")
            pipe.pexpire(self.event_key, self.HISTORY_MS)
            pipe.execute()

    def events(self, since: int, until: int, now_ms: int) -> list[dict]:
        if since < now_ms - self.HISTORY_MS:
            raise SwarmError("verdict log window exceeds seven day retention")
        if since >= until or until > now_ms:
            raise SwarmError("verdict log needs --since before --until and --until no later than now")
        return [json.loads(raw)[1] for raw in self.redis.zrangebyscore(self.event_key, since, f"({until}")]
