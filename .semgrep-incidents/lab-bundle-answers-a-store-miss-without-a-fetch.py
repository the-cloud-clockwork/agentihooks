import subprocess
from pathlib import Path
from typing import Any, Protocol


class Stores(Protocol):
    def _holding(self, sha: str) -> Path | None: ...


class AnswersFromTheStoresAlone(Stores):
    # ruleid: lab-bundle-answers-a-store-miss-without-a-fetch
    def bundle(self, sha: str) -> bytes | None:
        self: Any
        if not self.root.is_dir():
            return None
        for store in sorted(self.root.iterdir()):
            found = subprocess.run(["git", "-C", str(store), "cat-file", "-t", sha], capture_output=True, text=True)
            if found.stdout.strip() == "commit":
                return self._bundled(store, sha)
        return None


class AnswersNoneOnTheFirstMiss(Stores):
    # ruleid: lab-bundle-answers-a-store-miss-without-a-fetch
    def bundle(self, sha: str) -> bytes | None:
        self: Any
        store = self._holding(sha)
        if store is None:
            return None
        return self._bundled(store, sha)


class FetchesACommitNoStoreHolds(Stores):
    # ok: lab-bundle-answers-a-store-miss-without-a-fetch
    def bundle(self, sha: str) -> bytes | None:
        self: Any
        store = self._holding(sha) or self._fetched(sha)
        if store is None:
            return None
        return self._bundled(store, sha)
