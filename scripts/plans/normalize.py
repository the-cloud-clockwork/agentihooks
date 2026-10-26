from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


class PlanError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Normalized:
    kind: str
    title: str
    markdown: str
    content: dict
    slices: dict[str, list[str]]


def normalize(data: bytes | str, name: str, existing: Iterable[str] = ()) -> Normalized:
    raise NotImplementedError
