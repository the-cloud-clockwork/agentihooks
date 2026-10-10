import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from scripts.swarm.store import SwarmError

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
COMMIT = re.compile(r"[0-9a-f]{40}")
CHALLENGE = re.compile(r'(\w+)="([^"]*)"')
TIMEOUT_SECONDS = 10

Opener = Callable[..., object]


class ImageUnresolved(SwarmError):
    pass


def is_tag(tag: str) -> bool:
    return TAG.fullmatch(tag) is not None and COMMIT.fullmatch(tag) is None and "sha256" not in tag


def _head(url: str, grant: str, opener: Opener) -> str:
    request = urllib.request.Request(url, method="HEAD")
    request.add_header("Accept", ACCEPT)
    if grant:
        request.add_header("Authorization", f"Bearer {grant}")
    with opener(request, timeout=TIMEOUT_SECONDS) as response:
        return response.headers.get("Docker-Content-Digest") or ""


def _grant(challenge: str, host: str, image: str, opener: Opener) -> str:
    fields = dict(CHALLENGE.findall(challenge))
    realm = fields.pop("realm", "")
    where = urllib.parse.urlsplit(realm)
    if (where.scheme, where.hostname) != ("https", host):
        raise ImageUnresolved(f"the registry for {image} names a token service other than https://{host}")
    with opener(urllib.request.Request(f"{realm}?{urllib.parse.urlencode(fields)}"), timeout=TIMEOUT_SECONDS) as answer:
        body = json.loads(answer.read())
    if not isinstance(body, dict):
        return ""
    return body.get("token") or body.get("access_token") or ""


def resolve(repository: str, tag: str, opener: Opener = urllib.request.urlopen) -> str:
    host, _, name = repository.partition("/")
    url, image = f"https://{host}/v2/{name}/manifests/{tag}", f"{repository}:{tag}"
    try:
        try:
            digest = _head(url, "", opener)
        except urllib.error.HTTPError as refused:
            if refused.code != 401:
                raise
            challenge = refused.headers.get("WWW-Authenticate") or ""
            if not challenge.startswith("Bearer "):
                raise ImageUnresolved(f"the registry for {image} asks for credentials the controller does not hold")
            digest = _head(url, _grant(challenge, host, image, opener), opener)
    except urllib.error.HTTPError as refused:
        raise ImageUnresolved(f"the registry answered {refused.code} for {image}") from None
    except (OSError, ValueError, http.client.HTTPException):
        raise ImageUnresolved(f"the registry for {image} is unreachable") from None
    if not DIGEST.fullmatch(digest):
        raise ImageUnresolved(f"the registry gave no sha256 digest for {image}")
    return digest
