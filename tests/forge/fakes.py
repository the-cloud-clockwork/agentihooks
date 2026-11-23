import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Frepo/merge_requests"
HEADERS = {"PRIVATE-TOKEN": "fake-token", "Content-Type": "application/json"}


def fixture(name: str) -> str:
    """Return the text of one recorded forge fixture."""
    return (FIXTURES / name).read_text()


class Ran:
    """A fake subprocess runner that answers scripted exit codes and stdout and records every call."""

    def __init__(self, *results: tuple[int, str]) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        code, out = self.results.pop(0)
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="boom\n" if code else "")


class Sent:
    """A fake HTTP transport that answers scripted JSON bodies and records every request."""

    def __init__(self, *bodies: object) -> None:
        self.bodies = [json.dumps(body).encode() for body in bodies]
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> bytes:
        self.calls.append((method, url, dict(headers), data))
        return self.bodies.pop(0)
