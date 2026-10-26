"""Compare feed only, scoped and graph expanded brain context packs on a held out corpus.

The kernel ranking is not importable here, so the packers follow its contract: scope applies before scoring and graph
expansion, heat never admits a candidate on its own, only accepted edges expand and superseded guidance yields to its
successor. A pack is graded by human labels in the corpus, never by how many arcs or tokens it carries.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

CORPUS = Path(__file__).parent / "corpus.json"
_WORD = re.compile(r"[a-z0-9]{3,}")
STOPWORDS = frozenset("after and are can does for how its own should that the this was what when why with".split())
EXPANDING = frozenset({"resolved_by", "decided_by", "related"})
GAINS = ("context_source_recall", "reviewed_usefulness", "repeated_mistakes_avoided")
LIMITS = ("scope_leakage", "irrelevant_injection", "stale_injection")
GRAPH_DECAY = 0.5
INFERRED_FACTOR = 0.5


@dataclass(frozen=True)
class Policy:
    name: str
    feed: bool = False
    hops: int = 0
    statuses: tuple[str, ...] = ("accepted",)
    relations: frozenset = EXPANDING
    current_only: bool = True
    heat_fill: bool = False
    limit: int | None = None
    tokens: int | None = None

    def describe(self) -> dict:
        return {
            "name": self.name,
            "feed": self.feed,
            "hops": self.hops,
            "statuses": list(self.statuses),
            "relations": sorted(self.relations),
            "current_only": self.current_only,
            "heat_fill": self.heat_fill,
            "limit": self.limit,
            "tokens": self.tokens,
        }


POLICIES = {
    "feed_only": Policy("feed_only", feed=True, current_only=False),
    "scoped": Policy("scoped"),
    "graph": Policy("graph", hops=1),
    "graph_wide": Policy(
        "graph_wide",
        hops=2,
        statuses=("accepted", "proposed"),
        relations=EXPANDING | {"supersedes"},
        current_only=False,
        limit=12,
        tokens=1800,
    ),
    "scoped_padded": Policy("scoped_padded", heat_fill=True, limit=12, tokens=1800),
}


class Refused(Exception):
    pass


def load_corpus(path: Path = CORPUS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def terms(*texts: str) -> frozenset[str]:
    return frozenset(word for text in texts for word in _WORD.findall(text.lower()) if word not in STOPWORDS)


def visible(policy: Policy, fixture: dict) -> dict[str, dict]:
    granted = set(fixture["grant"]["projects"])
    return {item["id"]: item for item in fixture["items"] if policy.feed or item["project"] in granted}


def current(items: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    omitted = sorted(ident for ident, item in items.items() if item.get("superseded_by") in items)
    return {ident: item for ident, item in items.items() if ident not in omitted}, omitted


def relevance(query: str, items: dict[str, dict]) -> dict[str, float]:
    wanted = terms(query)
    scores = {}
    for ident, item in items.items():
        shared = wanted & terms(item["title"], item["summary"])
        if shared:
            scores[ident] = len(shared) / len(wanted)
    return scores


def _reach(edge: dict, scores: dict[str, float], items: dict[str, dict], found: dict[str, float]) -> None:
    factor = GRAPH_DECAY * (INFERRED_FACTOR if edge["confidence"] == "inferred" else 1.0)
    for near, far in ((edge["source"], edge["target"]), (edge["target"], edge["source"])):
        value = scores.get(near, 0.0) * factor
        if far in items and far not in scores and value > found.get(far, 0.0):
            found[far] = value


def expand(policy: Policy, scores: dict[str, float], edges: list[dict], items: dict[str, dict]) -> dict[str, float]:
    scores = dict(scores)
    for _ in range(policy.hops):
        found: dict[str, float] = {}
        for edge in edges:
            if edge["status"] in policy.statuses and edge["relation"] in policy.relations:
                _reach(edge, scores, items, found)
        scores.update(found)
    return scores


def _by_heat(items: dict[str, dict], idents) -> list[str]:
    return sorted(idents, key=lambda ident: (-items[ident]["heat"], ident))


def ranked(policy: Policy, task: dict, items: dict[str, dict], edges: list[dict]) -> list[str]:
    if policy.feed:
        return _by_heat(items, items)
    scores = expand(policy, relevance(task["query"], items), edges, items)
    order = sorted(scores, key=lambda ident: (-scores[ident], ident))
    if policy.heat_fill:
        order += _by_heat(items, [ident for ident in items if ident not in scores])
    return order


def pack(policy: Policy, task: dict, fixture: dict, budget: dict) -> dict:
    items = visible(policy, fixture)
    omitted: list[str] = []
    if policy.current_only:
        items, omitted = current(items)
    limit, ceiling = policy.limit or budget["limit"], policy.tokens or budget["tokens"]
    selected, tokens, over = [], 0, 0
    for ident in ranked(policy, task, items, fixture["edges"]):
        if len(selected) == limit:
            break
        cost = items[ident]["tokens"]
        if tokens + cost > ceiling:
            over += 1
            continue
        selected.append(ident)
        tokens += cost
    return {"selected": selected, "tokens": tokens, "omitted_superseded": len(omitted), "omitted_over_budget": over}


def score(task: dict, packed: dict, fixture: dict) -> dict:
    granted = set(fixture["grant"]["projects"])
    projects = {item["id"]: item["project"] for item in fixture["items"]}
    inside = [ident for ident in packed["selected"] if projects[ident] in granted]
    labels = task["labels"]
    useful = sum(labels.get(ident) == "useful" for ident in inside)
    stale = sum(labels.get(ident) == "stale" for ident in inside)
    found = sum(ident in packed["selected"] for ident in task["expected"])
    avoided = found == len(task["expected"]) and not stale
    return {
        "case": task["case"],
        "injected": inside,
        "expected": len(task["expected"]),
        "found": found,
        "useful": useful,
        "stale_injection": stale,
        "irrelevant_injection": len(inside) - useful - stale,
        "scope_leakage": len(packed["selected"]) - len(inside),
        "arcs": len(packed["selected"]),
        "context_tokens": packed["tokens"],
        "omitted_superseded": packed["omitted_superseded"],
        "omitted_over_budget": packed["omitted_over_budget"],
        "repeated_mistake_avoided": avoided if task["repeated_mistake"] else None,
    }


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def totals(scored: dict[str, dict]) -> dict:
    rows = list(scored.values())
    sums = {key: sum(row[key] for row in rows) for key in ("expected", "found", "useful", "arcs", "context_tokens")}
    sums.update({key: sum(row[key] for row in rows) for key in LIMITS})
    mistakes = [row["repeated_mistake_avoided"] for row in rows if row["repeated_mistake_avoided"] is not None]
    return {
        **sums,
        "context_source_recall": _ratio(sums["found"], sums["expected"]),
        "reviewed_usefulness": _ratio(sums["useful"], sums["arcs"]),
        "repeated_mistakes_avoided": _ratio(sum(mistakes), len(mistakes)),
        "context_tokens_per_found_source": round(sums["context_tokens"] / sums["found"], 1) if sums["found"] else None,
    }


def evaluate(name: str, fixture_name: str, corpus: dict) -> dict:
    policy, fixture = POLICIES[name], corpus["fixtures"][fixture_name]
    scored = {
        task["id"]: score(task, pack(policy, task, fixture, corpus["budget"]), fixture) for task in fixture["tasks"]
    }
    return {
        "corpus_revision": corpus["corpus_revision"],
        "fixture": fixture_name,
        "actor": fixture["grant"]["actor"],
        "policy": policy.describe(),
        "tasks": scored,
        "totals": totals(scored),
    }


def digest(report: dict) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def qualify(accepted: dict, candidate: dict) -> dict:
    gains = [metric for metric in GAINS if candidate[metric] > accepted[metric]]
    regressions = [metric for metric in GAINS if candidate[metric] < accepted[metric]]
    regressions += [metric for metric in LIMITS if candidate[metric] > accepted[metric]]
    if candidate["scope_leakage"] and "scope_leakage" not in regressions:
        regressions.append("scope_leakage")
    reason = "regressed" if regressions else "qualified" if gains else "no_gain"
    return {
        "qualified": reason == "qualified",
        "reason": reason,
        "gains": gains,
        "regressions": regressions,
        "volume_delta": {key: candidate[key] - accepted[key] for key in ("arcs", "context_tokens")},
    }


class Registry:
    """The accepted ranking and packing policy for one corpus revision, replaced only by compare and set."""

    def __init__(self, path: Path):
        self.path = path
        self.staged = path.with_name(path.name + ".staged")

    def read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def seed(self, corpus_revision: str, policy: str, report_sha256: str) -> dict:
        if self.path.exists():
            raise Refused("already_seeded")
        entry = {
            "operation_id": "seed",
            "kind": "seed",
            "sequence": 1,
            "policy": policy,
            "report_sha256": report_sha256,
        }
        record = {"corpus_revision": corpus_revision, **entry, "history": [entry]}
        self.publish(self.stage(record))
        return record

    def recover(self) -> dict:
        discarded = self.staged.exists()
        if discarded:
            self.staged.unlink()
        return {"discarded_staged_write": discarded, "sequence": self.read()["sequence"]}

    def _replayed(self, record: dict, request: dict) -> dict | None:
        earlier = next((e for e in record["history"] if e["operation_id"] == request["operation_id"]), None)
        if earlier is None:
            return None
        fields = ("kind", "policy", "report_sha256")
        if [earlier[key] for key in fields] != [request[key] for key in fields]:
            raise Refused("operation_id_reused")
        return {"state": "replayed", "sequence": earlier["sequence"]}

    def _admit(self, record: dict, request: dict) -> None:
        if request["corpus_revision"] != record["corpus_revision"]:
            raise Refused("other_corpus_revision")
        if request["expected_sequence"] != record["sequence"]:
            raise Refused("stale_sequence")
        if request["kind"] == "promote" and not (request.get("verdict") or {}).get("qualified"):
            raise Refused("not_qualified")
        if request["kind"] == "revert":
            earlier = {e["policy"]: e["report_sha256"] for e in record["history"]}
            if request["policy"] not in earlier:
                raise Refused("never_accepted")
            if earlier[request["policy"]] != request["report_sha256"]:
                raise Refused("irreproducible_report")

    def plan(self, request: dict) -> dict:
        record = self.read()
        replayed = self._replayed(record, request)
        if replayed:
            return replayed
        self._admit(record, request)
        entry = {
            "operation_id": request["operation_id"],
            "kind": request["kind"],
            "sequence": record["sequence"] + 1,
            "policy": request["policy"],
            "report_sha256": request["report_sha256"],
        }
        return {"state": "accepted", "record": {**record, **entry, "history": [*record["history"], entry]}}

    def stage(self, record: dict) -> Path:
        with self.staged.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return self.staged

    def publish(self, staged: Path) -> None:
        os.replace(staged, self.path)

    def submit(self, request: dict) -> dict:
        planned = self.plan(request)
        if planned["state"] == "replayed":
            return planned
        self.publish(self.stage(planned["record"]))
        return {"state": "accepted", "sequence": planned["record"]["sequence"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", default="alpha")
    parser.add_argument("--policy", action="append", choices=sorted(POLICIES))
    args = parser.parse_args(argv)
    corpus = load_corpus()
    names = args.policy or ["feed_only", "scoped", "graph"]
    totals_by_policy = {name: evaluate(name, args.fixture, corpus)["totals"] for name in names}
    json.dump(totals_by_policy, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
