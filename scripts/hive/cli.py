"""agentihooks hive invite|join|revoke|controller|serve|set|show|list|run|install|bundle-origin."""

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, MutableMapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from scripts.hive import auth, bundle_origin, registry, server
from scripts.swarm_ledger import ledger_ca

LOCK_FILE = "hive.lock"

if TYPE_CHECKING:
    from redis import Redis


def redis_client() -> "Redis":
    from scripts.swarm.store import redis_client as connect

    return connect()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _join(url: str, code: str) -> dict:
    return _post(url, server.JOIN_PATH, {"code": code}, "join")


def refresh_env(environ: MutableMapping[str, str]) -> str:
    saved = _saved_env(environ)
    url, credential = (
        environ.get(name) or saved.get(name) for name in ("AGENTIHOOKS_HIVE_URL", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL")
    )
    if not (url and credential):
        raise auth.HiveError(
            "this host has not joined a hive: "
            "AGENTIHOOKS_HIVE_URL and AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL must both be set"
        )
    grant = _post(url, server.REFRESH_PATH, {"ledger_credential": credential}, "refresh")
    auth.write_env(registry.home(environ), url, {**grant, "ledger_credential": credential})
    ledger_ca.place(registry.home(environ), grant.get("ledger_ca"), environ)
    environ["AGENTIHOOKS_HIVE_REDIS_URL"] = grant["redis_url"]
    return grant["id"]


def saved_redis_url(environ: Mapping[str, str]) -> str | None:
    return _saved_env(environ).get("AGENTIHOOKS_HIVE_REDIS_URL") or None


def _saved_env(environ: Mapping[str, str]) -> dict[str, str]:
    try:
        lines = (registry.home(environ) / auth.ENV_FILE).read_text().splitlines()
    except OSError:
        return {}
    return dict(line.partition("=")[::2] for line in lines)


@contextlib.contextmanager
def refresh_lock(environ: Mapping[str, str]) -> Iterator[None]:
    path = registry.home(environ) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _post(url: str, path: str, body: dict, action: str) -> dict:
    parts = urlsplit(url)
    if parts.scheme != "https" and not server.is_loopback(parts.hostname):
        raise auth.HiveError(f"a {action} off loopback carries credentials, so the hive URL must be https")
    request = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=server.REQUEST_TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            reason = json.load(exc)["error"]
        except (ValueError, KeyError, TypeError):
            reason = f"HTTP {exc.code}"
        raise auth.HiveError(reason) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise auth.HiveError(f"the hive at {url} is unreachable ({exc})") from exc
    except ValueError as exc:
        raise auth.HiveError(f"the hive at {url} answered with a body that is not JSON") from exc


def _serve(args: argparse.Namespace) -> int:
    from scripts.swarm.store import redis_url

    if bool(args.tls_cert) != bool(args.tls_key):
        raise auth.HiveError("--tls-cert and --tls-key go together")
    tls = (args.tls_cert, args.tls_key) if args.tls_cert else None
    httpd = server.make_server(
        redis_client(), args.redis_url or redis_url(os.environ), args.host, args.port, tls, ledger_ca=ledger_ca.served()
    )
    scheme = "https" if tls else "http"
    print(f"hive join endpoint on {scheme}://{args.host}:{httpd.server_address[1]}{server.JOIN_PATH}", flush=True)
    httpd.serve_forever()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks hive", description="Hive credentials for remote swarm hosts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("invite", help="Print a one-time join code, valid fifteen minutes").add_argument("name")
    join = sub.add_parser("join", help="Exchange a join code for credentials in hive.env")
    join.add_argument("url")
    join.add_argument("code")
    sub.add_parser("refresh", help="Mint a new Redis credential for this hive through its join service")
    sub.add_parser("revoke", help="Delete a member's ledger credential and Redis user").add_argument("id")
    sub.add_parser(
        "controller", help="Write a new controller service credential to a private file, retiring the previous one"
    )
    serve = sub.add_parser("serve", help="Run the join endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--redis-url", help="Redis URL members connect to; defaults to this host's")
    serve.add_argument("--tls-cert", help="PEM certificate; required off loopback")
    serve.add_argument("--tls-key", help="PEM private key for --tls-cert")
    settings = sub.add_parser("set", help=f"Set {', '.join(registry.SETTINGS)} as key=value")
    settings.add_argument("id")
    settings.add_argument("settings", nargs="*", metavar="key=value")
    sub.add_parser("show", help="Print a hive's record as JSON").add_argument("id")
    sub.add_parser("list", help="One line per hive with its liveness")
    sub.add_parser("run", help="Publish hive telemetry every fifteen seconds")
    sub.add_parser("install", help="Write and enable the hive user service")
    compare = sub.add_parser("compare", help="List each live hive's configuration against a base hive's")
    compare.add_argument("--base", help="The base hive id; defaults to this machine's hive")
    origin = sub.add_parser(
        "bundle-origin", help="Write the linked bundle's git address and branch into the hive settings record"
    )
    origin.add_argument("--path", default=bundle_origin.SETTINGS_PATH, help="OpenBao record under the secret mount")
    return parser


def _list() -> None:
    now = now_ms()
    for record in registry.hives(redis_client()):
        state = "live" if registry.live(record, now) else "stale"
        roles = ",".join(record["roles"])
        print(f"{record['id']}\t{state}\tui={record['ui']}\troles={roles}\tmax-agents={record['max_agents']}")


def _registry(args: argparse.Namespace) -> None:
    if args.command == "set":
        print(json.dumps(registry.update(redis_client(), args.id, args.settings)))
    elif args.command == "show":
        record = registry.show(redis_client(), args.id)
        if record is None:
            raise auth.HiveError(f"no hive {args.id}")
        print(json.dumps(record))
    else:
        _list()


def _daemon(command: str) -> int:
    from scripts.hive import daemon

    actions = {"run": lambda: daemon.run(redis_client), "install": daemon.install}
    result = actions[command]()
    return 1 if result is False else 0


def _compare(base_id: str | None) -> int:
    from scripts.hive import config
    from scripts.swarm import commands

    redis, now = redis_client(), now_ms()
    base_id = base_id or commands.hive_id()
    found = {
        record["id"]: config.read(redis, record["id"]) for record in registry.hives(redis) if registry.live(record, now)
    }
    base = found.pop(base_id, None)
    if base is None:
        raise auth.HiveError(f"live hive {base_id} published no configuration")
    print(f"{base_id}\tbase\t{config.summary(base)}")
    differing = [hive_id for hive_id, other in sorted(found.items()) if _report(hive_id, base, other)]
    return 1 if differing else 0


def _report(hive_id: str, base: dict, other: dict | None) -> bool:
    from scripts.hive import config

    if other is None:
        print(f"{hive_id}\tno configuration published")
        return True
    lines = config.differences(base, other)
    print(f"{hive_id}\t{'differs' if lines else 'equal'}\t{config.summary(other)}")
    for line in lines:
        print(f"  {line}")
    return bool(lines)


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "invite":
            print(auth.invite(redis_client(), args.name))
        elif args.command == "join":
            grant = _join(args.url, args.code)
            path = auth.write_env(registry.home(), args.url, grant)
            ca = ledger_ca.place(registry.home(), grant.get("ledger_ca"))
            print(
                f"joined the hive as {grant['id']}; credentials are in {path}"
                + (f"; the ledger CA is in {ca}" if ca else "")
            )
        elif args.command == "refresh":
            with refresh_lock(os.environ):
                member_id = refresh_env(os.environ)
            print(
                f"refreshed the Redis credential of hive member {member_id}; it is in {registry.home() / auth.ENV_FILE}"
            )
        elif args.command == "revoke":
            auth.revoke(redis_client(), args.id)
            print(f"revoked {args.id}")
        elif args.command in ("run", "install"):
            return _daemon(args.command)
        elif args.command == "compare":
            return _compare(args.base)
        elif args.command == "controller":
            path = auth.write_controller_env(registry.home(), auth.issue_controller(redis_client()))
            print(f"issued a controller credential; it is in {path}")
        elif args.command in ("set", "show", "list"):
            _registry(args)
        elif args.command == "bundle-origin":
            print(bundle_origin.publish(os.environ, args.path))
        else:
            return _serve(args)
    except auth.HiveError as exc:
        print(f"hive {args.command} refused: {exc}", file=sys.stderr)
        return 1
    return 0
