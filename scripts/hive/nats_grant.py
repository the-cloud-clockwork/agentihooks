"""Signed NATS user credentials for hive members: minted by hive serve, revoked through an account JWT update."""

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import openbao_kv
from scripts.events import nkey, subjects
from scripts.events.config import SHARED_SLOT_BUCKETS
from scripts.events.dead import DEAD_STREAM
from scripts.hive import auth, buckets

if TYPE_CHECKING:
    from redis import Redis

SIGNER_ENV = "AGENTIHOOKS_NATS_SIGNER"
FILE_SCHEME = "file:"
HUB_PATH = "agentihooks/hub/"
TRUSTED_OPERATOR_ENV = "AGENTIHOOKS_NATS_TRUSTED_OPERATOR"
ACCOUNT_NAME = "SWARM"
REVOKED = f"{auth.PREFIX}:nats-revoked"
CLAIMS_UPDATE = "$SYS.REQ.CLAIMS.UPDATE"
PUSH_TIMEOUT_S = 10
UNLIMITED = {"subs": -1, "data": -1, "payload": -1}
ACCOUNT_LIMITS = {
    **UNLIMITED,
    "imports": -1,
    "exports": -1,
    "wildcards": True,
    "conn": -1,
    "leaf": -1,
    "mem_storage": -1,
    "disk_storage": -1,
    "streams": -1,
    "consumer": -1,
}
MEMBER_PUBLISH = (
    "swarm.*.execution.>",
    "dead.*",
    "dead.*.*",
    "$JS.API.INFO",
    f"$JS.API.STREAM.MSG.GET.{DEAD_STREAM}",
    "$JS.API.CONSUMER.INFO.>",
    "$JS.API.CONSUMER.MSG.NEXT.>",
    "$JS.ACK.>",
    *(f"$KV.{bucket}.>" for bucket in buckets.NAMES),
    *(f"$JS.API.STREAM.INFO.KV_{bucket}" for bucket in buckets.NAMES),
    *(f"$JS.API.STREAM.MSG.GET.KV_{bucket}" for bucket in buckets.NAMES),
)


@dataclass(frozen=True)
class Signer:
    operator: nkey.KeyPair
    account: nkey.KeyPair
    system_credentials: str = field(repr=False)
    url: str
    trusted_operator: str
    member_swarms: Mapping[str, tuple[str, ...]] | None = None


def member_swarms(value) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or any(not isinstance(swarms, (list, tuple)) for swarms in value.values()):
        raise auth.HiveError("member_swarms must map member ids to lists of swarm names")
    try:
        for swarms in value.values():
            for swarm in swarms:
                subjects.subject(swarm, "execution")
    except ValueError as exc:
        raise auth.HiveError(f"member_swarms contains an invalid swarm name ({exc})") from None
    return {member: tuple(swarms) for member, swarms in value.items()}


def swarm_publish(swarm: str) -> list[str]:
    resources = (
        "EVENTS",
        "DEAD",
        *(f"QUEUE_{lane}" for lane in ("eng", "ci", "plan")),
        *(f"STATE_{bucket}" for bucket in buckets.NAMES),
    )
    streams = [subjects.stream_name(swarm, resource) for resource in resources]
    return [
        f"{subjects.PREFIX}.{swarm}.>",
        *(
            subject
            for stream in streams
            for subject in (
                f"$JS.API.STREAM.INFO.{stream}",
                f"$JS.API.STREAM.MSG.GET.{stream}",
                f"$JS.API.CONSUMER.INFO.{stream}.*",
                f"$JS.API.CONSUMER.MSG.NEXT.{stream}.*",
                f"$JS.ACK.{stream}.>",
            )
        ),
    ]


def from_environ(environ: Mapping[str, str]) -> Signer | None:
    path = environ.get(SIGNER_ENV)
    if not path:
        return None
    if path.startswith(FILE_SCHEME):
        return _from_file(path.removeprefix(FILE_SCHEME), environ)
    hub, _, name = path.partition(HUB_PATH)
    if hub or "%" in name or any(part in ("", ".", "..") for part in name.split("/")):
        raise auth.HiveError(
            f"the NATS signer record {path} holds the system account credential, "
            f"so it must live under the OpenBao path {HUB_PATH} that only the hub role reads"
        )
    trusted = _trusted(environ)
    bao = openbao_kv.from_env(environ)
    if bao is None:
        raise auth.HiveError(
            f"{SIGNER_ENV} names an OpenBao record, so {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must be set"
        )
    try:
        record, _ = openbao_kv.read(bao, path)
        return _signer(record, trusted)
    except (openbao_kv.OpenBaoError, KeyError, nkey.NkeyError) as exc:
        raise auth.HiveError(f"the NATS signer record {path} cannot be used ({exc})") from None


def _trusted(environ: Mapping[str, str]) -> str:
    trusted = environ.get(TRUSTED_OPERATOR_ENV)
    if not trusted:
        raise auth.HiveError(
            f"{SIGNER_ENV} is set, so {TRUSTED_OPERATOR_ENV} must name the operator public key the NATS server trusts"
        )
    return trusted


def _signer(record: Mapping, trusted: str) -> Signer:
    return Signer(
        nkey.from_seed(record["operator_seed"]),
        nkey.from_seed(record["account_seed"]),
        record["system_credentials"],
        record["url"],
        trusted,
        member_swarms(record["member_swarms"]) if "member_swarms" in record else None,
    )


def _from_file(path: str, environ: Mapping[str, str]) -> Signer:
    trusted = _trusted(environ)
    try:
        record = {found.name: found.read_text() for found in Path(path).iterdir() if found.is_file()}
        if "member_swarms" in record:
            record["member_swarms"] = json.loads(record["member_swarms"])
        return _signer(record, trusted)
    except (OSError, ValueError, KeyError, TypeError, nkey.NkeyError) as exc:
        raise auth.HiveError(f"the NATS signer file {path} cannot be used ({exc})") from None


def user_claims() -> dict:
    return {"type": "user", "version": 2, "pub": {}, "sub": {}, **UNLIMITED}


def member_claims(user: str, swarms: tuple[str, ...] | None = None) -> dict:
    if swarms is None:
        return {
            **user_claims(),
            "pub": {"allow": list(MEMBER_PUBLISH)},
            "sub": {"allow": [f"{subjects.inbox(user)}.>"]},
        }
    shared = [
        permission
        for bucket in SHARED_SLOT_BUCKETS
        for permission in (
            f"$KV.{bucket}.>",
            f"$JS.API.STREAM.INFO.KV_{bucket}",
            f"$JS.API.STREAM.MSG.GET.KV_{bucket}",
        )
    ]
    publish = [*shared, *(subject for swarm in swarms for subject in swarm_publish(swarm))] if swarms else []
    return {
        **user_claims(),
        "pub": {"allow": publish} if publish else {"deny": [">"]},
        "sub": {"allow": [*(f"{subjects.PREFIX}.{swarm}.>" for swarm in swarms), f"{subjects.inbox(user)}.>"]},
    }


def member_user(signer: Signer, member_id: str) -> nkey.KeyPair:
    return nkey.derive(signer.account, nkey.USER, member_id)


def require_listed(signer: Signer, member_id: str) -> None:
    if signer.member_swarms is not None and member_id not in signer.member_swarms:
        raise auth.HiveError(
            f"the hub record lists no member swarms for hive member {member_id}; it mints no unscoped bus credential"
        )


def mint(signer: Signer, member_id: str) -> str:
    user = member_user(signer, member_id)
    require_listed(signer, member_id)
    swarms = None if signer.member_swarms is None else signer.member_swarms[member_id]
    claims = member_claims(user.public, swarms)
    return nkey.creds(nkey.encode_jwt(signer.account, user.public, f"hive-{member_id}", claims), user.seed)


def account_jwt(operator: nkey.KeyPair, account: nkey.KeyPair, revocations: Mapping[str, int]) -> str:
    claims = {"type": "account", "version": 2, "limits": ACCOUNT_LIMITS, "default_permissions": {"pub": {}, "sub": {}}}
    if revocations:
        claims["revocations"] = dict(revocations)
    return nkey.encode_jwt(operator, account.public, ACCOUNT_NAME, claims)


def push(signer: Signer, jwt: str) -> None:
    import nats as nats_client

    from scripts.events.nats import auth_options

    async def send() -> dict:
        nc = await nats_client.connect(
            signer.url,
            allow_reconnect=False,
            connect_timeout=PUSH_TIMEOUT_S,
            **auth_options(signer.system_credentials),
        )
        try:
            reply = await nc.request(CLAIMS_UPDATE, jwt.encode(), timeout=PUSH_TIMEOUT_S)
        finally:
            await nc.close()
        return json.loads(reply.data)

    answer = asyncio.run(send())
    if "error" in answer:
        raise auth.HiveError(f"NATS refused the account update ({answer['error'].get('description')})")


def revoke(redis: "Redis", signer: Signer, member_id: str) -> str:
    redis.hset(REVOKED, member_user(signer, member_id).public, int(time.time()))
    jwt = account_jwt(
        signer.operator, signer.account, {key: int(value) for key, value in redis.hgetall(REVOKED).items()}
    )
    issuer = nkey.claims(jwt)["iss"]
    if issuer != signer.trusted_operator:
        raise auth.HiveError(
            f"refusing to push an account update issued by {issuer}, "
            f"which is not the trusted operator {signer.trusted_operator}"
        )
    push(signer, jwt)
    return f"revoked the NATS credential of {member_id}"
