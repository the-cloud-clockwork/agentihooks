import ipaddress
import socket
from collections.abc import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.herdr.attach import Lookup
from scripts.swarm_v2.kubernetes.terminal import HostKeys

URL_ENVS = ("AGENTIHOOKS_HIVE_REDIS_URL", "AGENTIHOOKS_SWARM_REDIS_URL")
SCHEME = "rediss"
PORT = 6379
OPTIONS = {
    "decode_responses": True,
    "ssl_cert_reqs": "required",
    "ssl_check_hostname": True,
    "socket_connect_timeout": 3,
    "socket_timeout": 10,
}


class TransportRefused(SwarmError):
    pass


def _route(host: str, port: int, lookup: Lookup | None) -> None:
    try:
        found = (lookup or socket.getaddrinfo)(host, port, type=socket.SOCK_STREAM)
    except OSError:
        found = []
    addresses = {info[4][0] for info in found}
    if not addresses:
        raise TransportRefused(f"{host} has no route")
    if any(not ipaddress.ip_address(address).is_private for address in addresses):
        raise TransportRefused(f"{host} resolves outside the private network")


def private_url(environ: Mapping[str, str], lookup: Lookup | None = None) -> str:
    urls = [environ[name] for name in URL_ENVS if environ.get(name)]
    if not urls:
        raise TransportRefused(f"the publication transport needs {' or '.join(URL_ENVS)}")
    url = urls[0]
    parts = urlsplit(url)
    if parts.scheme != SCHEME:
        raise TransportRefused(f"the publication transport uses {SCHEME} only, not {parts.scheme}")
    # redis-py lets URL query options override the TLS options passed to from_url.
    if any(name.startswith("ssl_") for name, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise TransportRefused(
            "the publication transport sets its own TLS checks, so its URL may carry no ssl_ options"
        )
    if not parts.hostname:
        raise TransportRefused("the publication transport URL names no host")
    try:
        port = parts.port or PORT
    except ValueError:
        raise TransportRefused("the publication transport URL names an invalid port") from None
    _route(parts.hostname, port, lookup)
    return url


def connect(environ: Mapping[str, str], lookup: Lookup | None = None, factory: Callable | None = None) -> RedisStore:
    from redis import Redis, RedisError

    url = private_url(environ, lookup)
    try:
        client = (factory or Redis.from_url)(url, **OPTIONS)
        client.ping()
    except RedisError as error:
        host = urlsplit(url).hostname
        raise TransportRefused(
            f"the private swarm Redis at {host} refused the connection: {type(error).__name__}"
        ) from None
    return RedisStore(client)


def host_keys(environ: Mapping[str, str], lookup: Lookup | None = None, factory: Callable | None = None) -> HostKeys:
    return HostKeys(connect(environ, lookup, factory))
