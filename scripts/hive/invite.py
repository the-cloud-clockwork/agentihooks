"""agentihooks hive invite: mint a hive member and write its credential into the OpenBao record a hive mounts."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts import openbao_kv
from scripts.hive import auth, member_swarms

if TYPE_CHECKING:
    from redis import Redis

CREDENTIAL = "credential"
SWARM_URL = "swarm_url"
CA_PEM = "ca_pem"
HIVE_URL = "AGENTIHOOKS_HIVE_URL"
REDIS_URL = "AGENTIHOOKS_HIVE_REDIS_URL"
HIVE_ID = "AGENTIHOOKS_HIVE_ID"


@dataclass(frozen=True)
class Request:
    label: str
    path: str
    like: str | None = None
    url: str | None = None
    redis_url: str | None = None
    swarm_url: str | None = None
    ledger_ca: str | None = None
    bus_url: str | None = None
    signer_path: str | None = None
    swarms: tuple[str, ...] = ()


def default_path(label: str) -> str:
    return f"k8s/agentihooks-hive-{label}"


def invite(redis: "Redis", bao: openbao_kv.OpenBao, request: Request, local_redis_url: str) -> str:
    current = _read(bao, request.path)
    like = _read(bao, request.like) if request.like else {}
    if request.like and not like:
        raise auth.HiveError(f"the record {request.like} that --like names is empty or missing")
    sample = _env(like.get(CREDENTIAL))
    url = request.url or sample.get(HIVE_URL)
    if not url:
        raise auth.HiveError("the hive URL is unknown: pass --url or --like with a hive record")
    entries = {
        SWARM_URL: request.swarm_url or like.get(SWARM_URL),
        CA_PEM: like.get(CA_PEM) or request.ledger_ca,
    }
    entries = {key: value for key, value in entries.items() if value}
    grant = auth.mint(redis, request.label, request.redis_url or sample.get(REDIS_URL) or local_redis_url)
    member_settings = {"AGENTIHOOKS_SWARM_URL": entries.get(SWARM_URL)}
    bus_url = request.bus_url or sample.get("AGENTIHOOKS_BUS_URL")
    if bus_url:
        member_settings.update(AGENTIHOOKS_BUS="nats", AGENTIHOOKS_BUS_URL=bus_url)
    entries[CREDENTIAL] = auth.env_text(url, grant) + "".join(
        f"{key}={value}\n" for key, value in member_settings.items() if value
    )
    try:
        if request.signer_path:
            member_swarms.assign(bao, request.signer_path, [grant["id"]], request.swarms)
        total = openbao_kv.merge(bao, request.path, entries)
    except (openbao_kv.OpenBaoError, auth.HiveError) as exc:
        try:
            auth.revoke(redis, grant["id"])
        except auth.HiveError as revoked:
            raise auth.HiveError(f"{exc}; the new member {grant['id']} was not revoked ({revoked})") from None
        raise auth.HiveError(str(exc)) from None
    done = f"invited hive member {grant['id']} as {request.label}; {openbao_kv.summary(request.path, entries, total)}"
    previous = _env(current.get(CREDENTIAL)).get(HIVE_ID)
    if previous and previous != grant["id"]:
        try:
            auth.revoke(redis, previous)
        except auth.HiveError as exc:
            return f"{done}; the previous member was not revoked ({exc})"
        return f"{done}; revoked the previous member {previous}"
    return done


def _read(bao: openbao_kv.OpenBao, path: str) -> dict[str, str]:
    try:
        return openbao_kv.read(bao, path)[0]
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None


def _env(text: str | None) -> dict[str, str]:
    lines = text.splitlines() if text else []
    return dict(line.partition("=")[::2] for line in lines if "=" in line)
