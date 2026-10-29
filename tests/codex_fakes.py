import base64
import io
import json
import urllib.error

NOW = 1_800_000_000
DAY = 86400
BAO_URL = "https://bao.test"
BAO_TOKEN = "bao-token-value"
CREDS_URL = "/v1/secret/data/agentihooks/codex-creds"
CAS_CONFLICT = b'{"errors": ["check-and-set parameter did not match the current version"]}'


def jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def access_token(days_left: float, workspace: str = "ws-1", plan: str = "team", now: float = NOW) -> str:
    auth = {"chatgpt_account_id": workspace, "chatgpt_plan_type": plan}
    return jwt({"exp": int(now + days_left * DAY), "https://api.openai.com/auth": auth})


def http_error(url: str, code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body))


class FakeBao:
    """A KV v2 secret behind a urlopen-shaped callable."""

    def __init__(
        self, data=None, version=0, conflicts=0, url=BAO_URL, token=BAO_TOKEN, fail_writes=False, path=CREDS_URL
    ):
        self.path = path
        self.data = dict(data or {})
        self.version = version if data is None else max(version, 1)
        self.conflicts = conflicts
        self.url, self.token = url, token
        self.fail_writes = fail_writes
        self.garbage_writes = False
        self.empty_writes = False
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        assert timeout == 20
        assert request.full_url == f"{self.url.rstrip('/')}{self.path}"
        assert request.get_header("X-vault-token") == self.token
        if request.get_method() == "GET":
            if not self.version:
                raise http_error(request.full_url, 404, b'{"errors": []}')
            return io.BytesIO(json.dumps({"data": {"data": self.data, "metadata": {"version": self.version}}}).encode())
        if self.fail_writes:
            raise http_error(request.full_url, 503, b'{"errors": ["sealed"]}')
        if self.garbage_writes:
            return io.BytesIO(b"<html>proxy</html>")
        assert request.get_header("Content-type") == "application/json"
        body = json.loads(request.data)
        if self.conflicts:
            self.conflicts -= 1
            self.version += 1
            raise http_error(request.full_url, 400, CAS_CONFLICT)
        assert body["options"]["cas"] == self.version
        self.data, self.version = body["data"], self.version + 1
        if self.empty_writes:
            return io.BytesIO(b"")
        return io.BytesIO(json.dumps({"data": {"version": self.version}}).encode())

    @property
    def writes(self):
        return [request for request in self.requests if request.get_method() == "POST"]
