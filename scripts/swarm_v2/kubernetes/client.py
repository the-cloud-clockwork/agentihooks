"""Pod calls over the Kubernetes API server REST interface: an overloaded or failing server is ambiguous, never absent."""

import http.client
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TIMEOUT_SECONDS = 10
DELETABLE = ("pods", "services")
Opener = Callable[..., Any]


class AlreadyExists(Exception):
    pass


class PreconditionFailed(Exception):
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

    def create_config_map(self, body: dict) -> dict: ...


class KubeHttp:
    def __init__(
        self, server: str, token_path: Path, context: ssl.SSLContext, opener: Opener = urllib.request.urlopen
    ) -> None:
        self.server, self.token_path, self.context, self.opener = server, token_path, context, opener

    @classmethod
    def in_cluster(
        cls, environ: Mapping[str, str], account: Path = ACCOUNT, opener: Opener = urllib.request.urlopen
    ) -> "KubeHttp":
        host = environ["KUBERNETES_SERVICE_HOST"]
        host = f"[{host}]" if ":" in host else host
        context = ssl.create_default_context(cafile=str(account / "ca.crt"))
        return cls(f"https://{host}:{environ['KUBERNETES_SERVICE_PORT']}", account / "token", context, opener)

    def send(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        try:
            token = self.token_path.read_text().strip()
        except OSError:
            raise ConnectionError("the service account token is unreadable") from None
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.server + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS, context=self.context) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            except (ValueError, OSError, http.client.HTTPException):
                return error.code, {}
        except urllib.error.URLError as error:
            raise ConnectionError(str(error.reason)) from None
        except TimeoutError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise ConnectionError(f"the API server exchange failed: {type(error).__name__}") from None
        try:
            return status, json.loads(raw)
        except ValueError:
            raise ConnectionError("the API server answer is unreadable") from None


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

    def _path(self, name: str = "", kind: str = "pods") -> str:
        path = f"/api/v1/namespaces/{self.namespace}/{kind}"
        return f"{path}/{name}" if name else path

    def create_pod(self, body: dict) -> dict:
        return self._create("pods", body)

    def _create(self, kind: str, body: dict) -> dict:
        status, answer = self.http.send("POST", self._path(kind=kind), body)
        if status == 409 and answer.get("reason") == "AlreadyExists":
            raise AlreadyExists(body["metadata"]["name"])
        return _answer(status, answer)

    def read_pod(self, name: str) -> dict | None:
        return self.read("pods", name)

    def list_pods(self, selector: str) -> list[dict]:
        query = urllib.parse.urlencode({"labelSelector": selector})
        return _answer(*self.http.send("GET", f"{self._path()}?{query}"))["items"]

    def list_services(self, selector: str) -> list[dict]:
        query = urllib.parse.urlencode({"labelSelector": selector})
        return _answer(*self.http.send("GET", f"{self._path(kind='services')}?{query}"))["items"]

    def read(self, kind: str, name: str) -> dict | None:
        status, answer = self.http.send("GET", self._path(name, kind))
        return None if status == 404 else _answer(status, answer)

    def delete(self, kind: str, name: str, uid: str) -> bool:
        """False when the name is gone; PreconditionFailed when the name now holds another object."""
        if kind not in DELETABLE:
            raise ValueError("cleanup deletes only pods and services")
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
        status, answer = self.http.send("DELETE", self._path(name, kind), options)
        if status == 404:
            return False
        if status == 409:
            raise PreconditionFailed(name)
        if status not in (202, 204):
            _answer(status, answer)
        return True

    def create_config_map(self, body: dict) -> dict:
        return self._create("configmaps", body)
