from dataclasses import dataclass


@dataclass(frozen=True)
class Hand:
    handed: bool
    reason: str
    removed: bool = False


HANDED = Hand(True, "handed")
