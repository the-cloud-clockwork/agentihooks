import io
import json
import urllib.error
from email.message import Message

import pytest

from scripts.swarm_v2 import image_tag

pytestmark = pytest.mark.unit

REPOSITORY = "ghcr.io/the-cloud-clockwork/agentihooks-worker"
MANIFEST = "https://ghcr.io/v2/the-cloud-clockwork/agentihooks-worker/manifests/dev"
DIGEST = "sha256:" + "13" * 32
PULL = "-".join(("anonymous", "pull"))
CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:the-cloud-clockwork/agentihooks-worker:pull"'
ACCEPT = (
    "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
)


def _headers(**values):
    headers = Message()
    for name, value in values.items():
        headers[name.replace("_", "-")] = value
    return headers


class Response(io.BytesIO):
    def __init__(self, body=b"", **headers):
        super().__init__(body)
        self.headers = _headers(**headers)


class Registry:
    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _refused(code, **headers):
    return urllib.error.HTTPError(MANIFEST, code, "refused", _headers(**headers), None)


def _granted(field):
    return Response(json.dumps({field: PULL}).encode())


def test_an_anonymous_registry_answers_the_tag_digest_after_its_bearer_challenge():
    registry = Registry(
        _refused(401, WWW_Authenticate=CHALLENGE), _granted("token"), Response(Docker_Content_Digest=DIGEST)
    )

    assert image_tag.resolve(REPOSITORY, "dev", registry) == DIGEST

    (first, first_timeout), (grant, grant_timeout), (second, second_timeout) = registry.requests
    assert (first.full_url, first.get_method(), first.get_header("Accept")) == (MANIFEST, "HEAD", ACCEPT)
    assert first.get_header("Authorization") is None
    assert (grant.full_url, grant.get_method()) == (
        "https://ghcr.io/token?service=ghcr.io&scope=repository%3Athe-cloud-clockwork%2Fagentihooks-worker%3Apull",
        "GET",
    )
    assert (second.full_url, second.get_method(), second.get_header("Accept")) == (MANIFEST, "HEAD", ACCEPT)
    assert second.get_header("Authorization") == "Bearer " + PULL
    assert first_timeout == grant_timeout == second_timeout == 10


def test_an_open_registry_answers_the_digest_on_the_first_request():
    registry = Registry(Response(Docker_Content_Digest=DIGEST))

    assert image_tag.resolve(REPOSITORY, "dev", registry) == DIGEST
    assert len(registry.requests) == 1


def test_a_grant_answering_access_token_is_used():
    registry = Registry(
        _refused(401, WWW_Authenticate=CHALLENGE), _granted("access_token"), Response(Docker_Content_Digest=DIGEST)
    )

    assert image_tag.resolve(REPOSITORY, "dev", registry) == DIGEST
    assert registry.requests[2][0].get_header("Authorization") == "Bearer " + PULL


def test_a_missing_tag_is_refused_with_the_registry_answer():
    registry = Registry(_refused(404))

    with pytest.raises(image_tag.ImageUnresolved) as refused:
        image_tag.resolve(REPOSITORY, "dev", registry)

    assert str(refused.value) == f"the registry answered 404 for {REPOSITORY}:dev"


def test_a_refused_retry_after_the_challenge_is_refused_with_its_answer():
    registry = Registry(_refused(401, WWW_Authenticate=CHALLENGE), _granted("token"), _refused(403))

    with pytest.raises(image_tag.ImageUnresolved) as refused:
        image_tag.resolve(REPOSITORY, "dev", registry)

    assert str(refused.value) == f"the registry answered 403 for {REPOSITORY}:dev"


def test_a_registry_asking_for_credentials_is_refused():
    registry = Registry(_refused(401, WWW_Authenticate='Basic realm="ghcr.io"'))

    with pytest.raises(image_tag.ImageUnresolved) as refused:
        image_tag.resolve(REPOSITORY, "dev", registry)

    assert str(refused.value) == f"the registry for {REPOSITORY}:dev asks for credentials the controller does not hold"
    assert len(registry.requests) == 1


def test_an_unreachable_registry_is_refused():
    registry = Registry(urllib.error.URLError("no route to host"))

    with pytest.raises(image_tag.ImageUnresolved) as refused:
        image_tag.resolve(REPOSITORY, "dev", registry)

    assert str(refused.value) == f"the registry for {REPOSITORY}:dev is unreachable"


@pytest.mark.parametrize("digest", ["", "sha256:" + "1" * 63, "sha512:" + "1" * 64, "SHA256:" + "1" * 64])
def test_an_answer_without_a_sha256_digest_is_refused(digest):
    registry = Registry(Response(Docker_Content_Digest=digest))

    with pytest.raises(image_tag.ImageUnresolved) as refused:
        image_tag.resolve(REPOSITORY, "dev", registry)

    assert str(refused.value) == f"the registry gave no sha256 digest for {REPOSITORY}:dev"


def test_an_answer_with_no_digest_header_is_refused():
    registry = Registry(Response())

    with pytest.raises(image_tag.ImageUnresolved) as refused:
        image_tag.resolve(REPOSITORY, "dev", registry)

    assert str(refused.value) == f"the registry gave no sha256 digest for {REPOSITORY}:dev"


def test_a_grant_answer_that_is_not_json_is_refused_as_unreachable():
    registry = Registry(_refused(401, WWW_Authenticate=CHALLENGE), Response(b"<html>"))

    with pytest.raises(image_tag.ImageUnresolved) as refused:
        image_tag.resolve(REPOSITORY, "dev", registry)

    assert str(refused.value) == f"the registry for {REPOSITORY}:dev is unreachable"


@pytest.mark.parametrize("tag", ["dev", "sha-e04f98f3f", "v1.2_3", "_dev", "a" * 128])
def test_an_image_tag_is_accepted(tag):
    assert image_tag.is_tag(tag) is True


@pytest.mark.parametrize("tag", ["", "dev@sha256:" + "1" * 64, "sha256:" + "1" * 64, ".dev", "-dev", "a" * 129, "dé"])
def test_a_digest_or_malformed_tag_is_not_an_image_tag(tag):
    assert image_tag.is_tag(tag) is False
