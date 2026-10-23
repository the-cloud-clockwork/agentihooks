"""Signed NATS user credentials for hive members: minted by hive serve, revoked through an account JWT update."""

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scripts import openbao_kv
from scripts.events import nkey, subjects
from scripts.events.dead import DEAD_SUBJECT
from scripts.hive import auth

if TYPE_CHECKING:
    from redis import Redis

SIGNER_ENV = "AGENTIHOOKS_NATS_SIGNER"
HUB_PATH = "agentihooks/hub/"
TRUSTED_OPERATOR_ENV = "AGENTIHOOKS_NATS_TRUSTED_OPERATOR"
ACCOUNT_NAME = "SWARM"
REVOKED = f"{auth.PREFIX}:nats-revoked"
CLAIMS_UPDATE = "$SYS.REQ.CLAIMS.UPDATE"
PUSH_TIMEOUT_S = 10
UNLIMITED = {"subs": -1, "data": -1, "payload": -1}
OPEN = {"pub": {}, "sub": {}}
SWARM_SUBJECTS = f"{subjects.PREFIX}.>"
MEMBER_PUBLISH = [
    SWARM_SUBJECTS,
    DEAD_SUBJECT,
    "agentihooks.queue.>",
    "$KV.>",
    "$JS.ACK.>",
    "$JS.API.STREAM.CREATE.*",
    "$JS.API.STREAM.MSG.GET.*",
    "$JS.API.CONSUMER.INFO.*.*",
    "$JS.API.CONSUMER.DURABLE.CREATE.*.*",
    "$JS.API.CONSUMER.MSG.NEXT.*.*",
]
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


@dataclass(frozen=True)
class Signer:
    operator: nkey.KeyPair
    account: nkey.KeyPair
    system_credentials: str = field(repr=False)
    url: str
    trusted_operator: str


def from_environ(environ: Mapping[str, str]) -> Signer | None:
    path = environ.get(SIGNER_ENV)
    if not path:
        return None
    hub, _, name = path.partition(HUB_PATH)
    if hub or "%" in name or any(part in ("", ".", "..") for part in name.split("/")):
        raise auth.HiveError(
            f"the NATS signer record {path} holds the system account credential, "
            f"so it must live under the OpenBao path {HUB_PATH} that only the hub role reads"
        )
    trusted = environ.get(TRUSTED_OPERATOR_ENV)
    if not trusted:
        raise auth.HiveError(
            f"{SIGNER_ENV} is set, so {TRUSTED_OPERATOR_ENV} must name the operator public key the NATS server trusts"
        )
    bao = openbao_kv.from_env(environ)
    if bao is None:
        raise auth.HiveError(
            f"{SIGNER_ENV} names an OpenBao record, so {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must be set"
        )
    try:
        record, _ = openbao_kv.read(bao, path)
        return Signer(
            nkey.from_seed(record["operator_seed"]),
            nkey.from_seed(record["account_seed"]),
            record["system_credentials"],
            record["url"],
            trusted,
        )
    except (openbao_kv.OpenBaoError, KeyError, nkey.NkeyError) as exc:
        raise auth.HiveError(f"the NATS signer record {path} cannot be used ({exc})") from None


def user_claims(permissions: Mapping[str, dict] = OPEN) -> dict:
    return {"type": "user", "version": 2, **permissions, **UNLIMITED}


def member_user(signer: Signer, member_id: str) -> nkey.KeyPair:
    return nkey.derive(signer.account, nkey.USER, member_id)


def member_permissions(user: str) -> dict:
    return {"pub": {"allow": MEMBER_PUBLISH}, "sub": {"allow": [SWARM_SUBJECTS, f"{subjects.inbox(user)}.>"]}}


def mint(signer: Signer, member_id: str) -> str:
    user = member_user(signer, member_id)
    claims = user_claims(member_permissions(user.public))
    return nkey.creds(nkey.encode_jwt(signer.account, user.public, f"hive-{member_id}", claims), user.seed)


def account_jwt(signer: Signer, revocations: Mapping[str, int]) -> str:
    claims = {"type": "account", "version": 2, "limits": ACCOUNT_LIMITS, "default_permissions": {"pub": {}, "sub": {}}}
    if revocations:
        claims["revocations"] = dict(revocations)
    return nkey.encode_jwt(signer.operator, signer.account.public, ACCOUNT_NAME, claims)


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
    jwt = account_jwt(signer, {key: int(value) for key, value in redis.hgetall(REVOKED).items()})
    issuer = nkey.claims(jwt)["iss"]
    if issuer != signer.trusted_operator:
        raise auth.HiveError(
            f"refusing to push an account update issued by {issuer}, "
            f"which is not the trusted operator {signer.trusted_operator}"
        )
    push(signer, jwt)
    return f"revoked the NATS credential of {member_id}"
