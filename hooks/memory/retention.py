"""Retention policy per corpus and data class: an object is age eligible only under a policy declared for its corpus."""

from dataclasses import dataclass

CLASSES = ("raw_evidence", "native_resume", "preview", "vector", "operational_log")
CORPORA = ("personal", "swarm")
UNTIL_DELETED = "retain-until-deleted"
UNTIL_EXPIRY = "retain-until-expiry"
FIELDS = frozenset({"corpus", "version", "max_age_seconds"})


class PolicyError(ValueError):
    pass


def _age(value: object) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise PolicyError("a max age is a positive number of seconds, or null to keep the class")
    return value


@dataclass(frozen=True)
class Policy:
    corpus: str
    version: str
    max_age_seconds: tuple[tuple[str, int | None], ...]

    @classmethod
    def parse(cls, doc: object) -> "Policy":
        if not isinstance(doc, dict) or set(doc) != FIELDS:
            raise PolicyError("a retention policy names exactly its corpus, version and max age per class")
        if doc["corpus"] not in CORPORA:
            raise PolicyError("a retention policy corpus is personal or swarm")
        if not isinstance(doc["version"], str) or not doc["version"]:
            raise PolicyError("a retention policy carries a version")
        ages = doc["max_age_seconds"]
        if not isinstance(ages, dict) or set(ages) != set(CLASSES):
            raise PolicyError("a retention policy states a max age for every data class")
        return cls(doc["corpus"], doc["version"], tuple((name, _age(ages[name])) for name in CLASSES))

    def expired(self, data_class: str, created_at: float, now: float, retention: str = UNTIL_EXPIRY) -> bool:
        age = dict(self.max_age_seconds)[data_class]
        return retention == UNTIL_EXPIRY and age is not None and now - created_at >= age


@dataclass(frozen=True)
class Policies:
    """A corpus without its own declared policy keeps every object; a policy never applies to another corpus."""

    declared: tuple[Policy, ...]

    @classmethod
    def parse(cls, docs: object) -> "Policies":
        if not isinstance(docs, list):
            raise PolicyError("retention policies are a list with one policy per corpus")
        policies = tuple(Policy.parse(doc) for doc in docs)
        corpora = [policy.corpus for policy in policies]
        if len(corpora) != len(set(corpora)):
            raise PolicyError("each corpus declares at most one retention policy")
        return cls(policies)

    def get(self, corpus: str) -> Policy | None:
        return next((policy for policy in self.declared if policy.corpus == corpus), None)

    def versions(self) -> dict[str, str]:
        return {policy.corpus: policy.version for policy in self.declared}
