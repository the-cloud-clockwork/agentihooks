"""agentihooks hive invite|join|revoke|serve|set|show|list."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from scripts.hive import auth, registry, server

if TYPE_CHECKING:
    from redis import Redis


def redis_client() -> "Redis":
    from scripts.swarm.store import redis_client as connect

    return connect()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _join(url: str, code: str) -> dict:
    parts = urlsplit(url)
    if parts.scheme != "https" and not server.is_loopback(parts.hostname):
        raise auth.HiveError("a join off loopback carries credentials, so the hive URL must be https")
    request = urllib.request.Request(url.rstrip("/") + server.JOIN_PATH, data=json.dumps({"code": code}).encode())
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
    httpd = server.make_server(redis_client(), args.redis_url or redis_url(os.environ), args.host, args.port, tls)
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
    sub.add_parser("revoke", help="Delete a member's ledger credential and Redis user").add_argument("id")
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

    actions = {"run": lambda: daemon.run(redis_client()), "install": daemon.install}
    result = actions[command]()
    return 1 if result is False else 0


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "invite":
            print(auth.invite(redis_client(), args.name))
        elif args.command == "join":
            grant = _join(args.url, args.code)
            path = auth.write_env(registry.home(), args.url, grant)
            print(f"joined the hive as {grant['id']}; credentials are in {path}")
        elif args.command == "revoke":
            auth.revoke(redis_client(), args.id)
            print(f"revoked {args.id}")
        elif args.command in ("run", "install"):
            return _daemon(args.command)
        elif args.command in ("set", "show", "list"):
            _registry(args)
        else:
            return _serve(args)
    except auth.HiveError as exc:
        print(f"hive {args.command} refused: {exc}", file=sys.stderr)
        return 1
    return 0
