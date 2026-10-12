import socket

import pytest

from scripts.swarm_v2.herdr import publications
from scripts.swarm_v2.herdr.endpoints import Scope
from scripts.swarm_v2.herdr.publications import TransportRefused
from scripts.swarm_v2.kubernetes.terminal import HostKeys

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

HOST = "swarm.homeofanton.com"
SECRET = "fixture" + "-password"
URL = "rediss://hive-a:" + SECRET + "@" + HOST + ":6379/0"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey"


def hive(url: str) -> dict:
    return {"AGENTIHOOKS_HIVE_REDIS_URL": url}


def lookup_of(*addresses, seen=None):
    def lookup(host, port, type):  # noqa: A002
        if seen is not None:
            seen.append((host, port, type))
        return [(socket.AF_INET, type, 6, "", (address, port)) for address in addresses]

    return lookup


def no_dns(host, port, type):  # noqa: A002
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


class Factory:
    def __init__(self, error=None) -> None:
        import fakeredis

        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.error, self.calls = error, []

    def __call__(self, url, **options):
        self.calls.append((url, options))
        if self.error is not None:
            raise self.error
        return self.redis


def test_private_url_takes_the_hive_url_first():
    seen = []
    environ = {**hive(URL), "AGENTIHOOKS_SWARM_REDIS_URL": "rediss://other.test:6380/0"}

    assert publications.private_url(environ, lookup_of("10.10.30.207", seen=seen)) == URL
    assert seen == [(HOST, 6379, socket.SOCK_STREAM)]


def test_private_url_falls_back_to_the_swarm_url_and_its_default_port():
    seen = []
    url = f"rediss://{HOST}/0"

    assert publications.private_url({"AGENTIHOOKS_SWARM_REDIS_URL": url}, lookup_of("10.10.30.207", seen=seen)) == url
    assert seen == [(HOST, 6379, socket.SOCK_STREAM)]


@pytest.mark.parametrize("query", ["?socket_keepalive=true", "?sslnote=1"])
def test_private_url_keeps_other_query_options(query):
    url = URL + query

    assert publications.private_url(hive(url), lookup_of("10.10.30.207")) == url


@pytest.mark.parametrize(
    "environ, lookup, message",
    [
        (
            {},
            lookup_of("10.10.30.207"),
            "the publication transport needs AGENTIHOOKS_HIVE_REDIS_URL or AGENTIHOOKS_SWARM_REDIS_URL",
        ),
        (
            hive(URL.replace("rediss", "redis")),
            lookup_of("10.10.30.207"),
            "the publication transport uses rediss only, not redis",
        ),
        (
            hive("unix:///run/redis.sock"),
            lookup_of("10.10.30.207"),
            "the publication transport uses rediss only, not unix",
        ),
        (
            hive(URL + "?ssl_cert_reqs=none"),
            lookup_of("10.10.30.207"),
            "the publication transport sets its own TLS checks, so its URL may carry no ssl_ options",
        ),
        (
            hive(URL + "?decode_responses=1&ssl_check_hostname=false"),
            lookup_of("10.10.30.207"),
            "the publication transport sets its own TLS checks, so its URL may carry no ssl_ options",
        ),
        (hive(URL), no_dns, f"{HOST} has no route"),
        (hive(URL), lookup_of(), f"{HOST} has no route"),
        (hive(URL), lookup_of("10.10.30.207", "8.8.8.8"), f"{HOST} resolves outside the private network"),
        (hive("rediss:///0"), lookup_of("10.10.30.207"), "the publication transport URL names no host"),
        (
            hive(URL + "?ssl_cert_reqs="),
            lookup_of("10.10.30.207"),
            "the publication transport sets its own TLS checks, so its URL may carry no ssl_ options",
        ),
        (
            hive(f"rediss://{HOST}:port/0"),
            lookup_of("10.10.30.207"),
            "the publication transport URL names an invalid port",
        ),
    ],
)
def test_private_url_refuses_anything_but_verified_tls_to_a_private_address(environ, lookup, message):
    with pytest.raises(TransportRefused) as refused:
        publications.private_url(environ, lookup)

    assert str(refused.value) == message
    assert SECRET not in str(refused.value)


def test_connect_verifies_the_certificate_and_its_hostname():
    factory = Factory()

    store = publications.connect(hive(URL), lookup_of("10.10.30.207"), factory)

    assert store.redis is factory.redis
    assert factory.calls == [
        (
            URL,
            {
                "decode_responses": True,
                "ssl_cert_reqs": "required",
                "ssl_check_hostname": True,
                "socket_connect_timeout": 3,
                "socket_timeout": 10,
            },
        )
    ]


def test_connect_refuses_before_opening_an_insecure_url():
    factory = Factory()

    with pytest.raises(TransportRefused):
        publications.connect(hive(URL + "?ssl_cert_reqs=none"), lookup_of("10.10.30.207"), factory)

    assert factory.calls == []


def test_connect_and_host_keys_resolve_through_the_given_lookup():
    factory = Factory()
    public = lookup_of("8.8.8.8")

    for open_ in (publications.connect, publications.host_keys):
        with pytest.raises(TransportRefused) as refused:
            open_(hive(URL), public, factory)
        assert str(refused.value) == f"{HOST} resolves outside the private network"
    assert factory.calls == []


def test_connect_names_a_refused_connection_without_its_credential():
    import redis

    factory = Factory(redis.ConnectionError(f"certificate verify failed for {SECRET}"))

    with pytest.raises(TransportRefused) as refused:
        publications.connect(hive(URL), lookup_of("10.10.30.207"), factory)

    assert str(refused.value) == f"the private swarm Redis at {HOST} refused the connection: ConnectionError"


def test_controller_host_keys_reach_the_workstation_pinned():
    factory = Factory()
    HostKeys(publications.RedisStore(factory.redis)).record(Scope("controller", "rig", "exe-1", 4), HOST_KEY)

    keys = publications.host_keys(hive(URL), lookup_of("10.10.30.207"), factory)

    launched = keys.launched("rig", "exe-1")
    assert (launched.generation, launched.host_public) == (4, HOST_KEY)
