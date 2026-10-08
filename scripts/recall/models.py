from dataclasses import dataclass


@dataclass(frozen=True)
class RecallRecord:
    key: str
    ledger_slug: str
    swarm_slug: str
    kind: str
    ref: str
    parent_ref: str
    author: str
    time: int
    title: str
    text: str
    chunk_index: int
