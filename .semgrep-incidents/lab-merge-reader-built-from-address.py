from typing import Any, Protocol
from urllib.parse import urlsplit


class Resolving(Protocol):
    def __call__(self, repo: str, number: int, head: str) -> Any: ...


class ReaderFromTheAddress(Resolving):
    def __call__(self: Any, repo: str, number: int, head: str) -> Any:
        if repo not in self.readers:
            # ruleid: lab-merge-reader-built-from-address
            self.readers[repo] = self.reader(repo)
        return self.readers[repo], self.mirror(repo)


class ReaderFromTheAddressUnkept(Resolving):
    def __call__(self: Any, repo: str, number: int, head: str) -> Any:
        # ruleid: lab-merge-reader-built-from-address
        return self.reader(repo), self.mirror(repo)


class ReaderFromOwnerAndName(Resolving):
    def __call__(self: Any, repo: str, number: int, head: str) -> Any:
        if repo not in self.readers:
            # ok: lab-merge-reader-built-from-address
            self.readers[repo] = self.reader(urlsplit(repo).path.removeprefix("/"))
        return self.readers[repo], self.mirror(repo)
