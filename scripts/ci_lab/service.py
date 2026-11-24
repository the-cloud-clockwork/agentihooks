"""The scheduler port: the lease API workers call, GET /capacity from a load snapshot and GET /grades/<sha> with every
grade of one commit."""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from scripts.ci_lab import lease_api
from scripts.ci_lab.capacity import Load, document

__all__ = ["GRADES", "Routes", "serve"]

CAPACITY = "/capacity"
GRADES = "/grades/"
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class Routes:
    """What the scheduler port answers from: the lease API, the load snapshot and the grades of a commit."""

    lease: lease_api.Answer
    snapshot: Callable[[], Load | None]
    grades: Callable[[str], Sequence[Mapping[str, Any]]]

    def respond(self, method: str, target: str, raw: bytes, authorization: str | None) -> lease_api.Reply:
        """Answer GET /capacity and GET /grades/<sha> here and every other request through the lease API; capacity
        answers 503 before the first load snapshot is built."""
        path = urlsplit(target).path
        if method == "GET" and path == CAPACITY:
            load = self.snapshot()
            if load is None:
                return 503, {"error": "the load snapshot is not built yet"}
            return 200, document(load)
        if method == "GET" and path.startswith(GRADES):
            head = path.removeprefix(GRADES)
            if not SHA.fullmatch(head):
                return 400, {"error": "a grade lookup names one full commit sha"}
            return 200, {"head": head, "grades": list(self.grades(head))}
        return self.lease(method, target, raw, authorization)


def serve(host: str, port: int, routes: Routes) -> ThreadingHTTPServer:
    """Bind the scheduler port's routes on host and port; the caller runs serve_forever."""
    return lease_api.bind(host, port, routes.respond)
