"""Pod calls over the Kubernetes API server REST interface: an overloaded or failing server is ambiguous, never absent."""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TIMEOUT_SECONDS = 10


class AlreadyExists(Exception):
    pass


class ApiRefused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"API server refused with {status} {reason}")
        self.status, self.reason = status, reason


class PodApi(Protocol):
    namespace: str

    def create_pod(self, body: dict) -> dict: ...

    def read_pod(self, name: str) -> dict | None: ...

    def list_pods(self, selector: str) -> list[dict]: ...


class KubeHttp:
    def __init__(
        self, server: str, token_path: Path, context: ssl.SSLContext, opener: Callable = urllib.request.urlopen
    ):
        self.server, self.token_path, self.context, self.opener = server, token_path, context, opener

    @classmethod
    def in_cluster(
        cls, environ: Mapping[str, str], account: Path = ACCOUNT, opener: Callable = urllib.request.urlopen
    ) -> "KubeHttp":
        host = environ["KUBERNETES_SERVICE_HOST"]
        host = f"[{host}]" if ":" in host else host
        context = ssl.create_default_context(cafile=str(account / "ca.crt"))
        return cls(f"https://{host}:{environ['KUBERNETES_SERVICE_PORT']}", account / "token", context, opener)

    def send(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.server + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token_path.read_text().strip()}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS, context=self.context) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            except ValueError:
                return error.code, {}
        except urllib.error.URLError as error:
            raise ConnectionError(str(error.reason)) from None


def _answer(status: int, body: dict) -> dict:
    if status in (200, 201):
        return body
    if status == 429 or status >= 500:
        raise ConnectionError(f"API server answered {status}")
    raise ApiRefused(status, body.get("reason", ""))


class PodClient:
    """List calls carry no resourceVersion, so the API server answers from a quorum read."""

    def __init__(self, http: KubeHttp, namespace: str) -> None:
        self.http, self.namespace = http, namespace

    def _path(self, name: str = "") -> str:
        path = f"/api/v1/namespaces/{urllib.parse.quote(self.namespace, safe='')}/pods"
        return f"{path}/{urllib.parse.quote(name, safe='')}" if name else path

    def create_pod(self, body: dict) -> dict:
        status, answer = self.http.send("POST", self._path(), body)
        if status == 409 and answer.get("reason") == "AlreadyExists":
            raise AlreadyExists(body["metadata"]["name"])
        return _answer(status, answer)

    def read_pod(self, name: str) -> dict | None:
        status, answer = self.http.send("GET", self._path(name))
        return None if status == 404 else _answer(status, answer)

    def list_pods(self, selector: str) -> list[dict]:
        query = urllib.parse.urlencode({"labelSelector": selector})
        return _answer(*self.http.send("GET", f"{self._path()}?{query}"))["items"]
