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

from scripts.swarm_v2.kubernetes.watch import CursorExpired

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TIMEOUT_SECONDS = 10
DELETABLE = ("pods", "services")
NODES = "/api/v1/nodes"
WATCH_SECONDS = 1
GONE = 410
OBJECTS = {
    "Secret": ("api/v1", "secrets"),
    "Service": ("api/v1", "services"),
    "NetworkPolicy": ("apis/networking.k8s.io/v1", "networkpolicies"),
}
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

    def pod_logs(self, name: str, container: str) -> str: ...

    def list_pods(self, selector: str) -> list[dict]: ...

    def snapshot(self, selector: str) -> tuple[list[dict], str]: ...

    def watch_pods(self, selector: str, version: str) -> list[tuple[str, dict]]: ...

    def ready_nodes(self) -> list[str] | None: ...

    def list_quotas(self) -> list[dict]: ...

    def create_config_map(self, body: dict) -> dict: ...

    def create_object(self, body: dict) -> dict: ...

    def read(self, kind: str, name: str) -> dict | None: ...

    def delete(self, kind: str, name: str, uid: str) -> bool: ...


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

    def send(
        self, method: str, path: str, body: dict | None = None, read: Callable[[bytes], Any] = json.loads
    ) -> tuple[int, Any]:
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
            return status, read(raw)
        except ValueError:
            raise ConnectionError("the API server answer is unreadable") from None


def _answer(status: int, body: dict) -> dict:
    if status in (200, 201):
        return body
    if status == 429 or status >= 500:
        raise ConnectionError(f"API server answered {status}")
    raise ApiRefused(status, body.get("reason", ""))


def _items(answer: object) -> list[dict]:
    items = answer.get("items") if isinstance(answer, dict) else None
    if not isinstance(items, list):
        raise ConnectionError("the API server list answer carries no items")
    return items


def _events(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _ready(node: dict) -> bool:
    conditions = node.get("status", {}).get("conditions", [])
    return any(condition["type"] == "Ready" and condition["status"] == "True" for condition in conditions)


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
        return self._post(self._path(kind=kind), body)

    def _post(self, path: str, body: dict) -> dict:
        status, answer = self.http.send("POST", path, body)
        if status == 409 and answer.get("reason") == "AlreadyExists":
            raise AlreadyExists(body["metadata"]["name"])
        return _answer(status, answer)

    def create_object(self, body: dict) -> dict:
        if body.get("kind") not in OBJECTS:
            raise ValueError("the controller creates only Secret, Service and NetworkPolicy objects")
        group, plural = OBJECTS[body["kind"]]
        return self._post(f"/{group}/namespaces/{self.namespace}/{plural}", body)

    def read_pod(self, name: str) -> dict | None:
        return self.read("pods", name)

    def pod_logs(self, name: str, container: str) -> str:
        query = urllib.parse.urlencode(
            {"container": container, "tailLines": 200, "limitBytes": 65536, "timestamps": "true"}
        )
        status, answer = self.http.send(
            "GET", f"{self._path(name)}/log?{query}", read=lambda raw: raw.decode(errors="replace")
        )
        return _answer(status, answer)

    def list_pods(self, selector: str) -> list[dict]:
        return self.snapshot(selector)[0]

    def snapshot(self, selector: str) -> tuple[list[dict], str]:
        """The resource version is empty when the answer names none, so the caller lists again instead of watching."""
        query = urllib.parse.urlencode({"labelSelector": selector})
        answer = _answer(*self.http.send("GET", f"{self._path()}?{query}"))
        return _items(answer), answer.get("metadata", {}).get("resourceVersion", "")

    def watch_pods(self, selector: str, version: str) -> list[tuple[str, dict]]:
        """Raises CursorExpired when the API server no longer holds events after `version`."""
        query = urllib.parse.urlencode(
            {"labelSelector": selector, "watch": "1", "resourceVersion": version, "timeoutSeconds": WATCH_SECONDS}
        )
        status, events = self.http.send("GET", f"{self._path()}?{query}", read=_events)
        if status == GONE:
            raise CursorExpired(version)
        events = _answer(status, events)
        if any(event["type"] == "ERROR" for event in events):
            raise CursorExpired(version)
        return [(event["type"], event["object"]) for event in events]

    def ready_nodes(self) -> list[str] | None:
        """None when the nodes cannot be listed, so an unlisted node is never taken as proof of loss."""
        try:
            status, answer = self.http.send("GET", NODES)
            nodes = _items(answer)
        except ConnectionError:
            return None
        if status != 200:
            return None
        return [node["metadata"]["name"] for node in nodes if _ready(node)]

    def list_services(self, selector: str) -> list[dict]:
        query = urllib.parse.urlencode({"labelSelector": selector})
        return _items(_answer(*self.http.send("GET", f"{self._path(kind='services')}?{query}")))

    def list_quotas(self) -> list[dict]:
        return _items(_answer(*self.http.send("GET", self._path(kind="resourcequotas"))))

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
