import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO
from urllib.parse import quote, urlsplit

from redis.exceptions import RedisError

from scripts.swarm import stack_env, store
from scripts.swarm_ledger import ledger_link


def prepare(home: Path, environ: Mapping[str, str], out: TextIO) -> int:
    values = stack_env.read_env(home / ".env")
    if not values.get("SWARM_REDIS_PASSWORD"):
        print("Run agentihooks swarm init before preparing existing clients.", file=out)
        return 1
    if not ledger_link.local_mode(environ):
        print("Client preparation requires the existing local deployment.", file=out)
        return 1
    source = urlsplit(store.redis_url(environ))
    if source.scheme not in ("redis", "rediss") or source.hostname not in ("127.0.0.1", "localhost", "::1"):
        print("Client preparation requires the existing localhost Redis endpoint.", file=out)
        return 1
    address = source.netloc.rpartition("@")[2]
    endpoint = source._replace(netloc=f"swarm:{quote(values['SWARM_REDIS_PASSWORD'], safe='')}@{address}").geturl()
    try:
        client = store.redis_client({**environ, "AGENTIHOOKS_SWARM_REDIS_URL": endpoint})
        if client.acl_whoami() != "swarm":
            print("Redis did not authenticate the named swarm user.", file=out)
            return 1
        if "nopass" in client.acl_getuser("swarm")["flags"]:
            print("The named swarm user must require its bundle password.", file=out)
            return 1
        if not client.config_get("aclfile").get("aclfile"):
            print("Deploy the persistent Redis ACL file before activating clients.", file=out)
            return 1
        target = home.parent / "swarm-client.env"
        staged = target.with_suffix(".env.next")
        stack_env.write_private(staged, f"AGENTIHOOKS_SWARM_REDIS_URL={shlex.quote(endpoint)}\n")
        os.replace(staged, target)
    except (RedisError, OSError) as exc:
        print(f"Client preparation failed ({type(exc).__name__}); existing client settings are unchanged.", file=out)
        return 1
    print("Named Redis clients activated on the existing store; services, data and ports are unchanged.", file=out)
    return 0
