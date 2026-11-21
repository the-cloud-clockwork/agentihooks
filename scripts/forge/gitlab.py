"""GitLab merge requests read and commented on through the REST v4 API."""

from collections.abc import Callable, Mapping

from scripts.forge.model import ChangeRequest

HTTP_TIMEOUT_SEC = 60
PAGE_SIZE = 100
Transport = Callable[[str, str, Mapping[str, str], bytes | None], bytes]


def urlopen_transport(method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> bytes:
    raise NotImplementedError


class GitLabForge:
    def __init__(self, host: str, path: str, token: str, send: Transport = urlopen_transport) -> None:
        self.web = f"https://{host}/{path}"
        self.api = ""
        self.token = token
        self.send = send

    def read(self, number: int) -> ChangeRequest:
        raise NotImplementedError

    def post(self, number: int, body: str) -> str:
        raise NotImplementedError
