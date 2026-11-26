from typing import Protocol

REQUIRED = "required"


class Poster(Protocol):
    def post(self, head: str, gate: str, status: str, conclusion: str, run: str, shadow: bool) -> int: ...


class NoMemory:
    def __init__(self, poster: Poster) -> None:
        self.poster = poster

    def _fail(self, head: str, run: str) -> list[str]:
        # ruleid: lab-refusal-posted-twice-in-a-pass
        self.poster.post(head, REQUIRED, "completed", "failure", run, True)
        return []


class MemoryNeverFilled:
    def __init__(self, poster: Poster) -> None:
        self.poster = poster

    def _fail(self, head: str, run: str, said: set[tuple[str, str]]) -> list[str]:
        if (head, run) in said:
            return []
        # ruleid: lab-refusal-posted-twice-in-a-pass
        self.poster.post(head, REQUIRED, "completed", "failure", run, True)
        return []


class PostedBeforeTheLookUp:
    def __init__(self, poster: Poster) -> None:
        self.poster = poster

    def _fail(self, head: str, run: str, said: set[tuple[str, str]]) -> list[str]:
        # ruleid: lab-refusal-posted-twice-in-a-pass
        self.poster.post(head, REQUIRED, "completed", "failure", run, True)
        if (head, run) in said:
            return []
        said.add((head, run))
        return []


class PostedElsewhere:
    def __init__(self, poster: Poster) -> None:
        self.poster = poster
        self.said: set[tuple[str, str]] = set()

    def _refuse(self, head: str, run: str) -> list[str]:
        if (head, run) in self.said:
            return []
        self.said.add((head, run))
        # ruleid: lab-refusal-posted-twice-in-a-pass
        self.poster.post(head, REQUIRED, "completed", "failure", run, True)
        return []


class OncePerPass:
    def __init__(self, poster: Poster) -> None:
        self.poster = poster
        self.said: set[tuple[str, str]] = set()

    def _fail(self, head: str, run: str) -> list[str]:
        said = (head, run)
        if said in self.said:
            return []
        self.said.add(said)
        # ok: lab-refusal-posted-twice-in-a-pass
        self.poster.post(head, REQUIRED, "completed", "failure", run, True)
        return []


class AnotherCheck:
    def advance(self, poster: Poster, head: str, run: str) -> None:
        # ok: lab-refusal-posted-twice-in-a-pass
        poster.post(head, "lint", "completed", "failure", run, True)
