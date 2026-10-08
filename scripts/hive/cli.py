"""agentihooks hive invite|join|revoke|serve."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from scripts.hive import auth, server

if TYPE_CHECKING:
    from redis import Redis


def redis_client() -> "Redis":
    from scripts.swarm.store import redis_client as connect

    return connect()


def _home() -> Path:
    return Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")


def _join(url: str, code: str) -> dict:
    parts = urlsplit(url)
    if parts.scheme != "https" and not server.is_loopback(parts.hostname or ""):
        raise auth.HiveError("a join off loopback carries credentials, so the hive URL must be https")
    request = urllib.request.Request(
        url.rstrip("/") + server.JOIN_PATH,
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            reason = json.load(exc)["error"]
        except (ValueError, KeyError, TypeError):
            reason = f"HTTP {exc.code}"
        raise auth.HiveError(reason) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise auth.HiveError(f"the hive at {url} is unreachable ({exc})") from exc


def _serve(args: argparse.Namespace) -> int:
    from scripts.swarm.store import redis_url

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
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "invite":
            print(auth.invite(redis_client(), args.name))
        elif args.command == "join":
            grant = _join(args.url, args.code)
            path = auth.write_env(_home(), args.url, grant)
            print(f"joined the hive as {grant['id']}; credentials are in {path}")
        elif args.command == "revoke":
            auth.revoke(redis_client(), args.id)
            print(f"revoked {args.id}")
        else:
            return _serve(args)
    except auth.HiveError as exc:
        print(f"hive {args.command} refused: {exc}", file=sys.stderr)
        return 1
    return 0
