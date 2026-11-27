from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    head: str
    base: str


@dataclass(frozen=True)
class Gate:
    paths: Sequence[str]
    base_paths: Sequence[str]


def _within(files: Mapping[str, str], paths: Sequence[str]) -> dict[str, str]:
    return {path: blob for path, blob in files.items() if path in paths}


class Expander:
    def _files(self, mirror: str, sha: str) -> dict[str, str]:
        return {mirror: sha}

    def _diffed(self, mirror: str, request: Request, base: dict[str, str]) -> tuple[str, dict[str, str]]:
        return "", base

    def delivered(self, mirror: str, request: Request, gate: Gate) -> tuple[dict[str, str], dict[str, str]]:
        head, base = self._files(mirror, request.head), self._files(mirror, request.base)
        # ruleid: lab-static-base-read-at-delivered-base
        return _within(head, gate.paths), _within(base, gate.base_paths)

    def delivered_alone(self, mirror: str, request: Request, gate: Gate) -> dict[str, str]:
        base = self._files(mirror, request.base)
        # ruleid: lab-static-base-read-at-delivered-base
        return _within(base, gate.base_paths)

    def merged(self, mirror: str, request: Request, gate: Gate) -> tuple[dict[str, str], dict[str, str]]:
        head, base = self._files(mirror, request.head), self._files(mirror, request.base)
        _, diffed = self._diffed(mirror, request, base)
        # ok: lab-static-base-read-at-delivered-base
        return _within(head, gate.paths), _within(diffed, gate.base_paths)
